import { Archive, Check, ChevronLeft, ChevronRight, Hand, Link2, MoreHorizontal, Pencil, RotateCcw, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { type Approval, agentsApi } from "../../agentsApi";
import { api, getMe } from "../../api";
import { ago, LOCALE, label, t } from "../../i18n";
import { taskChanged, TaskLink, taskHref, useSheetOrder, withoutSheet } from "../../taskSheet";
import {
  type Actor,
  type Comment,
  PRIORITY_LABEL,
  type Related,
  type Summary,
  type Task,
  type TaskBrief,
  type TaskLive,
  type Version,
  dueLabel,
  tasksApi,
} from "../../tasksApi";
import { confirmDialog, toast } from "../overlay";
import AgentPicker from "./AgentPicker";
import { Avatar, StatusChip, statusText, toneOf } from "./bits";
import NeedsYou, { ApprovalCard, needsYou } from "./NeedsYou";
import { Discussion, HistoryTab, isTalk, Overview, Technical } from "./TaskTabs";

/* ------------------------------------------------------------------ shared lookups (loaded once) */

let meCache: Promise<{ id: number | null; owner: boolean }> | null = null;
const loadMe = () =>
  (meCache ??= getMe().then(
    (m) => ({ id: m.actor_id ?? null, owner: !!m.is_owner }),
    () => ({ id: null, owner: false }),
  ));
let actorsCache: Promise<Actor[]> | null = null;
const loadActors = () => (actorsCache ??= tasksApi.actors().catch(() => []));
type ProjectLite = { id: number; slug: string; name: string };
let projectsCache: Promise<ProjectLite[]> | null = null;
const loadProjects = () => (projectsCache ??= api<ProjectLite[]>("/api/projects").catch(() => []));

function useLoaded<T>(load: () => Promise<T>, init: T): T {
  const [v, setV] = useState<T>(init);
  useEffect(() => {
    let alive = true;
    load().then((x) => alive && setV(x));
    return () => {
      alive = false;
    };
  }, [load]);
  return v;
}

const editable = (el: EventTarget | null) =>
  el instanceof HTMLElement && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));

/* ------------------------------------------------------------------ the sheet */

/** A large side panel (78 % of the screen on a desktop, the whole screen on a phone). */
function Sheet({
  labelledBy,
  onClose,
  onStep,
  children,
}: {
  labelledBy: string;
  onClose: () => void;
  onStep: (dir: -1 | 1) => void;
  children: ReactNode;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      if (before && document.body.contains(before)) before.focus();
    };
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (document.querySelector("[role=alertdialog]")) return; // a confirmation dialog is on top
      if (e.key === "Escape") {
        if (editable(e.target) && (e.target as HTMLInputElement).value) {
          (e.target as HTMLElement).blur();
          return;
        }
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === "Tab" && box.current) {
        const f = [...box.current.querySelectorAll<HTMLElement>("a[href],button:not([disabled]),input,textarea,select,[tabindex]:not([tabindex='-1'])")].filter(
          (x) => x.offsetParent !== null,
        );
        if (!f.length) return;
        const [first, last] = [f[0], f[f.length - 1]];
        if (!box.current.contains(document.activeElement)) {
          e.preventDefault();
          first.focus();
        } else if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
        return;
      }
      if (editable(e.target) || e.altKey || e.ctrlKey || e.metaKey) return;
      if ((e.target as HTMLElement)?.closest?.("[role=tablist],[role=radiogroup],[role=menu]")) return;
      if (e.key === "ArrowLeft" || e.key === "k") onStep(-1);
      if (e.key === "ArrowRight" || e.key === "j") onStep(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onStep]);
  return (
    <div className="fixed inset-0 z-30 flex justify-end">
      <div aria-hidden className="sheet-backdrop absolute inset-0 bg-black/55" onClick={onClose} />
      <div
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        className="sheet-in relative flex h-full w-full flex-col border-l border-line bg-surface shadow-2xl lg:w-[78vw] lg:max-w-[1360px]"
      >
        {children}
      </div>
    </div>
  );
}

function TopBar({
  refText,
  pos,
  total,
  onStep,
  onClose,
  extra,
}: {
  refText: string;
  pos: number;
  total: number;
  onStep: (dir: -1 | 1) => void;
  onClose: () => void;
  extra?: ReactNode;
}) {
  return (
    <div className="flex h-14 shrink-0 items-center gap-2 border-b border-line px-3 sm:px-5">
      <button type="button" onClick={onClose} className="btn h-9! px-2.5!" aria-label={t("tk.close")}>
        <X size={16} /> <span className="hidden sm:inline">{t("tk.close")}</span>
      </button>
      <span className="font-mono text-xs text-ink-2">{refText}</span>
      {total > 1 && pos >= 0 && (
        <span className="ml-1 flex items-center gap-1">
          <button type="button" className="btn h-8! px-1.5!" disabled={pos <= 0} onClick={() => onStep(-1)} aria-label={t("tk.prev")} title={t("tk.prev_hint")}>
            <ChevronLeft size={16} />
          </button>
          <span className="text-xs text-ink-2 tabular-nums">
            {pos + 1} / {total}
          </span>
          <button type="button" className="btn h-8! px-1.5!" disabled={pos >= total - 1} onClick={() => onStep(1)} aria-label={t("tk.next")} title={t("tk.next_hint")}>
            <ChevronRight size={16} />
          </button>
        </span>
      )}
      <span className="ml-auto flex items-center gap-1.5">{extra}</span>
    </div>
  );
}

/* ------------------------------------------------------------------ meta column */

const SOURCE: Record<string, string> = {
  ui: "tk.src.ui",
  api: "tk.src.ui",
  capture: "tk.src.ui",
  mcp: "tk.src.agent",
  ask_owner: "tk.src.ask",
  gmail: "tk.src.email",
  email: "tk.src.email",
  scheduler: "tk.src.routine",
  schedule: "tk.src.routine",
  routine: "tk.src.routine",
  chat: "tk.src.chat",
};
const sourceWord = (s: string) => (SOURCE[s] ? t(SOURCE[s]) : s.replace(/_/g, " "));
/** An agent's role, when it says more than the name ("ceo" under "CEO" does not). */
const roleOf = (live: TaskLive | null, name: string | null) => {
  const r = live?.assignee?.role?.replace(/_/g, " ");
  return r && r.toLowerCase() !== (name ?? "").toLowerCase() ? r : null;
};
const date = (d: string) => new Date(d).toLocaleDateString(LOCALE, { day: "numeric", month: "numeric", year: "numeric" });

function MetaRow({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 py-2">
      <dt className="text-xs text-ink-2">{k}</dt>
      <dd className="min-w-0 text-[13px] break-words text-ink">{children}</dd>
    </div>
  );
}

function BriefLink({ b }: { b: TaskBrief }) {
  return (
    <TaskLink taskRef={b.ref} className="group flex min-w-0 items-center gap-2 rounded px-1 py-1 -mx-1 hover:bg-raised">
      <span className="font-mono text-xs text-ink-2">{b.ref}</span>
      <span className="min-w-0 flex-1 truncate text-[13px] group-hover:text-accent">{b.title}</span>
      <span className="sr-only">{statusText(b.status)}</span>
    </TaskLink>
  );
}

function Meta({ task, related, actors, live, onEdit }: { task: Task; related: Related | null; actors: Actor[]; live: TaskLive | null; onEdit: () => void }) {
  const projects = useLoaded(loadProjects, [] as ProjectLite[]);
  const project = projects.find((p) => p.id === task.project_id);
  const creator = actors.find((a) => a.id === task.created_by);
  const links: { k: string; items: TaskBrief[] }[] = [
    { k: t("tk.meta.parent"), items: task.parent ? [{ ...task.parent, status: "next" as const, assignee_name: null, assignee_type: null }] : [] },
    { k: t("tk.meta.ask_for"), items: related?.ask_for?.task ? [related.ask_for.task] : [] },
    { k: t("tk.meta.asks"), items: related?.asks_open ?? [] },
    { k: t("tk.meta.mentioned"), items: related?.mentioned ?? [] },
  ].filter((l) => l.items.length);
  return (
    <div className="flex flex-col">
      <div className="flex items-center pb-1">
        <h3 className="text-xs font-medium tracking-wide text-ink-2 uppercase">{t("tk.meta.title")}</h3>
        <button type="button" onClick={onEdit} className="ml-auto inline-flex items-center gap-1 text-xs text-accent hover:underline">
          <Pencil size={12} /> {t("tk.meta.edit")}
        </button>
      </div>
      <dl className="flex flex-col divide-y divide-line">
        <MetaRow k={t("tk.meta.assignee")}>
          <span className="flex items-center gap-2">
            <Avatar type={task.assignee_type} name={task.assignee_name} size={22} />
            <span className="min-w-0">
              {task.assignee_name === "Owner" ? t("who.me") : (task.assignee_name ?? t("who.unassigned"))}
              {roleOf(live, task.assignee_name) && <span className="block text-xs text-ink-2">{roleOf(live, task.assignee_name)}</span>}
            </span>
          </span>
        </MetaRow>
        {task.reviewer_name && <MetaRow k={t("tk.meta.reviewer")}>{task.reviewer_name === "Owner" ? t("who.me") : task.reviewer_name}</MetaRow>}
        {project && (
          <MetaRow k={t("tk.meta.project")}>
            <Link to={`/projects/${project.slug}`} className="text-accent hover:underline">
              {project.name}
            </Link>
          </MetaRow>
        )}
        {task.topic && <MetaRow k={t("tk.meta.topic")}>#{task.topic}</MetaRow>}
        {task.do_date && <MetaRow k={t("tk.meta.do_date")}>{date(task.do_date)}</MetaRow>}
        {task.status === "waiting" && task.follow_up && <MetaRow k={t("tk.meta.follow_up")}>{date(task.follow_up)}</MetaRow>}
        <MetaRow k={t("tk.meta.source")}>{sourceWord(task.source)}</MetaRow>
        <MetaRow k={t("tk.meta.created")}>
          {date(task.created_at)}
          {creator && <span className="text-ink-2"> · {creator.is_owner ? t("who.me") : creator.name}</span>}
        </MetaRow>
        <MetaRow k={t("tk.meta.updated")}>{ago(task.updated_at)}</MetaRow>
        {task.visibility === "private" && <MetaRow k={t("tk.meta.visibility")}>{t("work.visibility.private")}</MetaRow>}
      </dl>
      {links.map((l) => (
        <div key={l.k} className="flex flex-col border-t border-line pt-2 pb-1">
          <span className="pb-0.5 text-xs text-ink-2">{l.k}</span>
          {l.items.map((b) => (
            <BriefLink key={b.ref} b={b} />
          ))}
        </div>
      ))}
    </div>
  );
}

const inp = "h-9 min-w-0 w-full rounded-md border border-line bg-bg px-2.5 text-[13px] outline-none focus:border-accent";
const STATUSES = ["inbox", "next", "working", "review", "waiting", "someday", "done"] as const;

function EditField({ k, children }: { k: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-xs text-ink-2">{k}</span>
      {children}
    </label>
  );
}

/** Every field of the task, for when something needs changing (hidden until "Upravit"). */
function MetaEdit({ task, actors, save, onDone }: { task: Task; actors: Actor[]; save: (c: Record<string, unknown>) => void; onDone: () => void }) {
  const kindWord = (k: string) => (k === "ai" ? t("who.ai") : k === "human" ? t("who.person") : t("who.agent"));
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center">
        <h3 className="text-xs font-medium tracking-wide text-ink-2 uppercase">{t("tk.meta.editing")}</h3>
        <button type="button" onClick={onDone} className="btn-accent ml-auto h-8!">
          <Check size={14} /> {t("tk.meta.done")}
        </button>
      </div>
      <EditField k={t("tk.f.title")}>
        <input key={`t${task.updated_at}`} className={inp} defaultValue={task.title} onBlur={(e) => e.target.value.trim() && e.target.value !== task.title && save({ title: e.target.value })} />
      </EditField>
      <EditField k={t("tk.f.status")}>
        <select className={inp} value={task.status} onChange={(e) => save({ status: e.target.value })}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {statusText(s)}
            </option>
          ))}
        </select>
      </EditField>
      <EditField k={t("tk.meta.assignee")}>
        <select
          className={inp}
          value={task.assignee_type === "external" ? "" : (task.assignee_id ?? "")}
          onChange={(e) => save({ assignee: e.target.value ? { type: "human", id: Number(e.target.value) } : null })}
        >
          <option value="">{t("who.unassigned")}</option>
          {actors.map((a) => (
            <option key={a.id} value={a.id}>
              {a.is_owner ? t("who.me") : a.name} · {kindWord(a.kind)}
            </option>
          ))}
        </select>
      </EditField>
      <EditField k={t("tk.meta.reviewer")}>
        <select className={inp} value={task.reviewer_id ?? ""} onChange={(e) => save({ reviewer: e.target.value ? Number(e.target.value) : null })}>
          <option value="">{t("work.detail.reviewer_default", { name: task.reviewer_name ?? t("who.owner") })}</option>
          {actors
            .filter((a) => a.id !== task.assignee_id)
            .map((a) => (
              <option key={a.id} value={a.id}>
                {a.is_owner ? t("who.me") : a.name}
              </option>
            ))}
        </select>
      </EditField>
      <div className="grid grid-cols-2 gap-3">
        <EditField k={t("tk.f.priority")}>
          <select className={inp} value={task.priority ?? ""} onChange={(e) => save({ priority: e.target.value ? Number(e.target.value) : null })}>
            <option value="">{t("work.priority.unset")}</option>
            {[1, 2, 3].map((p) => (
              <option key={p} value={p}>
                P{p} · {PRIORITY_LABEL[p]}
              </option>
            ))}
          </select>
        </EditField>
        <EditField k={t("tk.f.deadline")}>
          <input type="date" className={inp} value={task.deadline ?? ""} onChange={(e) => save({ deadline: e.target.value || null })} />
        </EditField>
        <EditField k={t("tk.f.do_date")}>
          <input type="date" className={inp} value={task.do_date ?? ""} onChange={(e) => save({ do_date: e.target.value || null })} />
        </EditField>
        <EditField k={t("tk.f.topic")}>
          <input key={`p${task.updated_at}`} className={inp} defaultValue={task.topic ?? ""} onBlur={(e) => (e.target.value || null) !== task.topic && save({ topic: e.target.value || null })} />
        </EditField>
        <EditField k={t("work.detail.energy")}>
          <select className={inp} value={task.energy ?? ""} onChange={(e) => save({ energy: e.target.value || null })}>
            <option value="">{t("work.energy.opt.unset")}</option>
            <option value="high">{t("work.energy.opt.high")}</option>
            <option value="low">{t("work.energy.opt.low")}</option>
          </select>
        </EditField>
        <EditField k={t("work.detail.estimate")}>
          <input
            key={`e${task.updated_at}`}
            type="number"
            min={0}
            className={inp}
            defaultValue={task.estimate_min ?? ""}
            onBlur={(e) => save({ estimate_min: e.target.value ? Number(e.target.value) : null })}
          />
        </EditField>
        <EditField k={t("work.detail.value")}>
          <select className={inp} value={task.value_kind ?? ""} title={t("work.value.help")} onChange={(e) => save({ value_kind: e.target.value || null })}>
            <option value="">{t("work.value.auto", { v: t(`work.value.${task.value_kind_effective ?? "platform"}`) })}</option>
            <option value="business">{t("work.value.business")}</option>
            <option value="platform">{t("work.value.platform")}</option>
            <option value="demo">{t("work.value.demo_long")}</option>
          </select>
        </EditField>
        <EditField k={t("tk.f.visibility")}>
          <select className={inp} value={task.visibility} onChange={(e) => save({ visibility: e.target.value })}>
            <option value="team">{t("work.visibility.team")}</option>
            <option value="private">{t("work.visibility.private")}</option>
            <option value="public">{t("work.visibility.public")}</option>
          </select>
        </EditField>
        {task.status === "waiting" && (
          <EditField k={t("tk.meta.follow_up")}>
            <input type="date" className={inp} value={task.follow_up ?? ""} onChange={(e) => save({ follow_up: e.target.value || null })} />
          </EditField>
        )}
      </div>
      <EditField k={t("tk.dod")}>
        <textarea
          key={`d${task.updated_at}`}
          rows={3}
          defaultValue={task.definition_of_done ?? ""}
          onBlur={(e) => (e.target.value || null) !== task.definition_of_done && save({ definition_of_done: e.target.value || null })}
          className="rounded-md border border-line bg-bg p-2.5 text-[13px] outline-none focus:border-accent"
        />
      </EditField>
    </div>
  );
}

/* ------------------------------------------------------------------ the task panel */

function SummaryBox({ summary, loading }: { summary: Summary | null; loading: boolean }) {
  if (!summary?.text && !loading) return null;
  return (
    <section aria-labelledby="tk-summary" className="rounded-lg border border-line bg-raised/60 px-4 py-3.5">
      <h3 id="tk-summary" className="flex items-center gap-2 pb-1.5 text-xs font-medium tracking-wide text-ink-2 uppercase">
        {t("tk.summary")}
        {loading && <span className="breathe text-xs font-normal tracking-normal normal-case">{t("tk.summary_updating")}</span>}
      </h3>
      {summary?.text ? (
        <p className="text-[15px] leading-relaxed text-ink">{summary.text}</p>
      ) : (
        <div className="flex flex-col gap-2" aria-hidden>
          <span className="h-3 w-11/12 rounded bg-line" />
          <span className="h-3 w-3/4 rounded bg-line" />
        </div>
      )}
    </section>
  );
}

type Tab = "overview" | "talk" | "history" | "tech";

function MoreMenu({ items }: { items: { label: string; icon: ReactNode; onClick: () => void; hidden?: boolean }[] }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const off = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", off);
    return () => document.removeEventListener("mousedown", off);
  }, [open]);
  return (
    <div ref={box} className="relative">
      <button type="button" className="btn h-9! px-2!" aria-haspopup="menu" aria-expanded={open} aria-label={t("tk.more_actions")} onClick={() => setOpen(!open)}>
        <MoreHorizontal size={16} />
      </button>
      {open && (
        <div
          role="menu"
          className="panel absolute right-0 z-40 mt-1 flex w-56 flex-col p-1 shadow-lg"
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              setOpen(false);
            }
          }}
        >
          {items
            .filter((i) => !i.hidden)
            .map((i) => (
              <button
                key={i.label}
                role="menuitem"
                type="button"
                className="flex h-9 items-center gap-2.5 rounded px-2.5 text-left text-[13px] text-ink hover:bg-raised"
                onClick={() => {
                  setOpen(false);
                  i.onClick();
                }}
              >
                <span className="text-ink-2">{i.icon}</span>
                {i.label}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}

const TAB_IDS: Tab[] = ["overview", "talk", "history", "tech"];

function TaskPanel({
  taskRef,
  focus,
  initialTab,
  onClose,
  onStep,
  pos,
  total,
}: {
  taskRef: string;
  focus: string | null;
  initialTab: string | null;
  onClose: () => void;
  onStep: (d: -1 | 1) => void;
  pos: number;
  total: number;
}) {
  const startTab: Tab = TAB_IDS.includes(initialTab as Tab) ? (initialTab as Tab) : "overview";
  const me = useLoaded(loadMe, { id: null as number | null, owner: false });
  const actors = useLoaded(loadActors, [] as Actor[]);
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [related, setRelated] = useState<Related | null>(null);
  const [comments, setComments] = useState<Comment[] | null>(null);
  const [history, setHistory] = useState<Version[] | null>(null);
  const [live, setLive] = useState<TaskLive | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [tab, setTab] = useState<Tab>(startTab);
  const [editing, setEditing] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const focused = useRef<string | null>(null);

  const load = useCallback(() => {
    tasksApi.get(taskRef).then(
      (x) => {
        setTask(x);
        setError(null);
      },
      (e) => setError(e.message),
    );
    tasksApi.related(taskRef).then(setRelated, () => setRelated({ asks_open: [], ask_for: null, approvals: [], mentioned: [] }));
    tasksApi.comments(taskRef).then(setComments, () => setComments([]));
    tasksApi.history(taskRef).then(setHistory, () => setHistory([]));
    tasksApi.live(taskRef).then(setLive, () => setLive(null));
  }, [taskRef]);

  useEffect(() => {
    setTask(null);
    setRelated(null);
    setComments(null);
    setHistory(null);
    setSummary(null);
    setTab(startTab);
    setEditing(false);
    scroller.current?.scrollTo({ top: 0 });
    load();
  }, [taskRef, load]);

  // The TL;DR: the cached one at once, then a new one when the task changed since.
  useEffect(() => {
    if (!task) return;
    let alive = true;
    tasksApi.summary(task.ref, false).then((s) => {
      if (!alive) return;
      setSummary(s);
      if (!s.fresh) {
        setSummaryLoading(true);
        tasksApi
          .summary(task.ref, true)
          .then((n) => alive && setSummary(n), () => undefined)
          .finally(() => alive && setSummaryLoading(false));
      }
    }, () => undefined);
    return () => {
      alive = false;
      setSummaryLoading(false);
    };
  }, [task?.ref, task?.updated_at]);

  // Opened from "Čeká na tebe": straight to what needs you.
  useEffect(() => {
    if (!task || !related || focused.current === taskRef) return;
    focused.current = taskRef;
    const box = document.getElementById("needs-you");
    if (focus === "needs" && box) {
      box.scrollIntoView({ block: "start" });
      (box.querySelector<HTMLElement>("[data-needs-focus]") ?? box.querySelector<HTMLElement>("button,textarea"))?.focus({ preventScroll: true });
    } else document.getElementById("tk-title")?.focus({ preventScroll: true });
  }, [task, related, focus, taskRef]);

  const run = async (p: Promise<unknown>, done?: string) => {
    try {
      await p;
      if (done) toast(done);
      load();
      taskChanged();
      return true;
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
      return false;
    }
  };
  const save = (c: Record<string, unknown>) => run(tasksApi.update(taskRef, c));

  const talkCount = (comments ?? []).filter(isTalk).length;
  const top = (
    <TopBar
      refText={taskRef}
      pos={pos}
      total={total}
      onStep={onStep}
      onClose={onClose}
      extra={
        <button
          type="button"
          className="btn h-9! px-2.5!"
          title={t("tk.copy_link")}
          aria-label={t("tk.copy_link")}
          onClick={() => navigator.clipboard?.writeText(`${window.location.origin}/tasks/${taskRef}`).then(() => toast(t("tk.copied")))}
        >
          <Link2 size={15} />
        </button>
      }
    />
  );

  if (!task)
    return (
      <>
        {top}
        <div className="flex flex-col gap-4 p-8" aria-busy="true">
          {error ? (
            <p className="text-sm text-red-300">{error}</p>
          ) : (
            <>
              <span className="h-7 w-2/3 rounded bg-line" />
              <span className="h-4 w-1/3 rounded bg-line" />
              <span className="mt-6 h-24 w-full rounded bg-line/60" />
            </>
          )}
        </div>
      </>
    );

  const tone = toneOf(task, me.id);
  const due = dueLabel(task);
  const agentWork = task.assignee_type === "ai" || task.assignee_type === "agent";
  const waitsForMe = needsYou(task, related, me.id);
  const primary = (() => {
    if (waitsForMe)
      return (
        <button
          type="button"
          className="btn-accent h-9!"
          onClick={() => {
            const box = document.getElementById("needs-you");
            box?.scrollIntoView({ block: "start", behavior: "smooth" });
            (box?.querySelector<HTMLElement>("[data-needs-focus]") ?? box?.querySelector<HTMLElement>("button,textarea"))?.focus({ preventScroll: true });
          }}
        >
          <Hand size={15} /> {t("tk.act.handle")}
        </button>
      );
    if (task.status === "done")
      return (
        <button type="button" className="btn h-9!" onClick={() => run(tasksApi.update(task.ref, { status: "next" }), t("tk.reopened"))}>
          <RotateCcw size={14} /> {t("tk.act.reopen")}
        </button>
      );
    if (agentWork) return <AgentPicker task={task} trigger="button" align="right" onReassigned={() => run(Promise.resolve())} />;
    return (
      <button type="button" className="btn-accent h-9!" onClick={() => run(tasksApi.complete(task.ref), t("tk.completed"))}>
        <Check size={15} /> {t("tk.act.complete")}
      </button>
    );
  })();

  const more = (
    <MoreMenu
      items={[
        { label: t("tk.meta.edit"), icon: <Pencil size={14} />, onClick: () => setEditing(true) },
        {
          label: t("tk.act.complete"),
          icon: <Check size={14} />,
          hidden: task.status === "done" || (!agentWork && !waitsForMe),
          onClick: () => run(tasksApi.complete(task.ref), t("tk.completed")),
        },
        {
          label: t("tk.act.intervene"),
          icon: <Hand size={14} />,
          hidden: !agentWork || task.status === "done",
          onClick: async () => {
            const n = await confirmDialog({
              title: t("work.detail.intervene_title"),
              body: t("work.detail.intervene_hint"),
              confirm: t("work.detail.intervene_confirm"),
              reason: t("work.detail.intervene_ask"),
            });
            if (n !== null) run(tasksApi.intervene(task.ref, n));
          },
        },
        {
          label: t("act.archive"),
          icon: <Archive size={14} />,
          onClick: async () => {
            const ok = await confirmDialog({ title: t("tk.archive_ask", { title: task.title }), confirm: t("act.archive") });
            if (ok !== null && (await run(tasksApi.archive(task.ref), t("work.detail.archived")))) onClose();
          },
        },
      ]}
    />
  );

  const meta = editing ? (
    <MetaEdit task={task} actors={actors} save={save} onDone={() => setEditing(false)} />
  ) : (
    <Meta task={task} related={related} actors={actors} live={live} onEdit={() => setEditing(true)} />
  );

  const TABS: { id: Tab; text: string; n?: number }[] = [
    { id: "overview", text: t("tk.tab.overview") },
    { id: "talk", text: t("tk.tab.talk"), n: talkCount },
    { id: "history", text: t("tk.tab.history") },
    { id: "tech", text: t("tk.tab.tech") },
  ];

  return (
    <>
      {top}
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div className="mx-auto flex max-w-[1240px] flex-col gap-6 px-4 py-5 sm:px-8 sm:py-7">
          {/* Header */}
          <header className="flex flex-col gap-3">
            {task.parent && (
              <TaskLink taskRef={task.parent.ref} className="self-start text-xs text-ink-2 hover:text-accent">
                {t("tk.step_of", { ref: task.parent.ref, title: task.parent.title })}
              </TaskLink>
            )}
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start">
              <h2 id="tk-title" tabIndex={-1} className="min-w-0 flex-1 text-[22px] leading-tight font-normal tracking-[-0.01em] break-words outline-none sm:text-[26px]">
                {task.title}
              </h2>
              <div className="flex shrink-0 items-center gap-2">
                {primary}
                {more}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <StatusChip tone={tone} progress={task.status === "working" ? task.progress : null} size="md" />
              <span className="flex items-center gap-2 text-[13px]">
                <Avatar type={task.assignee_type} name={task.assignee_name} size={26} />
                <span className="flex flex-col leading-tight">
                  <span>{task.assignee_name === "Owner" ? t("who.me") : (task.assignee_name ?? t("who.unassigned"))}</span>
                  {roleOf(live, task.assignee_name) && <span className="text-xs text-ink-2">{roleOf(live, task.assignee_name)}</span>}
                </span>
              </span>
              {task.priority && (
                <span className={`text-[13px] ${task.priority === 1 ? "text-orange-200" : "text-ink-2"}`}>
                  P{task.priority} · {PRIORITY_LABEL[task.priority]}
                </span>
              )}
              {task.deadline && (
                <span className={`text-[13px] ${due.urgent ? "text-orange-200" : "text-ink-2"}`}>
                  {t("tk.deadline", { d: new Date(task.deadline).toLocaleDateString(LOCALE, { day: "numeric", month: "long" }) })}
                  {due.urgent ? ` · ${due.text}` : ""}
                </span>
              )}
            </div>
          </header>

          <details className="rounded-lg border border-line px-4 py-2 lg:hidden" open={editing || undefined}>
            <summary className="cursor-pointer py-1 text-[13px] text-ink-2">{t("tk.meta.mobile")}</summary>
            <div className="pt-2">{meta}</div>
          </details>

          <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_272px]">
            <div className="flex min-w-0 flex-col gap-6">
              <NeedsYou task={task} related={related} meId={me.id} onDone={load} />
              <SummaryBox summary={summary} loading={summaryLoading} />

              <div className="flex min-w-0 flex-col">
                <div
                  role="tablist"
                  aria-label={t("tk.tabs")}
                  className="flex gap-1 overflow-x-auto border-b border-line"
                  onKeyDown={(e) => {
                    const i = TABS.findIndex((x) => x.id === tab);
                    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
                    if (!d) return;
                    e.preventDefault();
                    const next = TABS[(i + d + TABS.length) % TABS.length].id;
                    setTab(next);
                    document.getElementById(`tab-${next}`)?.focus();
                  }}
                >
                  {TABS.map((x) => (
                    <button
                      key={x.id}
                      id={`tab-${x.id}`}
                      role="tab"
                      type="button"
                      aria-selected={tab === x.id}
                      aria-controls={`panel-${x.id}`}
                      tabIndex={tab === x.id ? 0 : -1}
                      onClick={() => setTab(x.id)}
                      className={`-mb-px flex h-11 shrink-0 items-center gap-1.5 border-b-2 px-3 text-sm ${
                        tab === x.id ? "border-accent text-ink" : "border-transparent text-ink-2 hover:text-ink"
                      } ${x.id === "tech" ? "ml-auto" : ""}`}
                    >
                      {x.text}
                      {x.n ? <span className="rounded-full bg-raised px-1.5 text-xs text-ink-2 tabular-nums">{x.n}</span> : null}
                    </button>
                  ))}
                </div>
                <div id={`panel-${tab}`} role="tabpanel" aria-labelledby={`tab-${tab}`} className="pt-5">
                  {tab === "overview" && <Overview task={task} onSave={save} onChange={load} />}
                  {tab === "talk" && (
                    <Discussion
                      task={task}
                      comments={comments}
                      meId={me.id}
                      onSent={() => tasksApi.comments(taskRef).then(setComments)}
                    />
                  )}
                  {tab === "history" && <HistoryTab history={history} comments={comments} actors={actors} />}
                  {tab === "tech" && (
                    <Technical task={task} live={live} history={history} onChange={load} onRestore={(v) => run(tasksApi.restore(task.ref, v), t("tk.restored", { n: v }))} />
                  )}
                </div>
              </div>
            </div>
            <aside className="hidden min-w-0 lg:block">
              <div className="sticky top-0">{meta}</div>
            </aside>
          </div>
        </div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ an approval that belongs to no task */

function ApprovalPanel({ id, onClose }: { id: number; onClose: () => void }) {
  const [a, setA] = useState<Approval | null | undefined>(undefined);
  const load = useCallback(() => agentsApi.approvals("all").then((xs) => setA(xs.find((x) => x.id === id) ?? null), () => setA(null)), [id]);
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (a) document.querySelector<HTMLElement>("#needs-you [data-needs-focus]")?.focus();
  }, [a]);
  const why = a ? String(a.details?.why ?? a.details?.reason ?? a.details?.summary ?? "") : "";
  const details = a ? Object.fromEntries(Object.entries(a.details ?? {}).filter(([k]) => !["why", "reason", "summary", "screenshot"].includes(k))) : {};
  return (
    <>
      <TopBar refText={t("tk.approval_ref", { id })} pos={-1} total={0} onStep={() => undefined} onClose={onClose} />
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-[860px] flex-col gap-6 px-4 py-6 sm:px-8">
          {a === undefined && <p className="text-sm text-ink-2">{t("act.loading")}</p>}
          {a === null && <p className="text-sm text-ink-2">{t("tk.approval_gone")}</p>}
          {a && (
            <>
              <header className="flex flex-col gap-3">
                <h2 id="tk-title" tabIndex={-1} className="text-[24px] leading-tight font-normal outline-none">
                  {label("approval", a.action)}
                </h2>
                <span className="flex flex-wrap items-center gap-3 text-[13px] text-ink-2">
                  <StatusChip tone={a.status === "pending" ? "you" : a.status === "approved" ? "done" : "someday"} size="md" />
                  <span className="flex items-center gap-2">
                    <Avatar type={a.requested_by_kind === "human" ? "human" : a.requested_by_kind === "ai" ? "ai" : "agent"} name={a.requested_by_name} size={24} />
                    {a.requested_by_name}
                  </span>
                  <span>{ago(a.created_at)}</span>
                  {a.task_ref && (
                    <TaskLink taskRef={a.task_ref} className="text-accent hover:underline">
                      {t("tk.approval_task", { ref: a.task_ref })}
                    </TaskLink>
                  )}
                </span>
              </header>
              {a.status === "pending" ? (
                <section id="needs-you" aria-labelledby="needs-you-title" className="flex flex-col gap-4 rounded-lg border border-orange-400/50 bg-orange-400/[0.06] p-4 sm:p-5">
                  <h3 id="needs-you-title" className="flex items-center gap-2 text-sm font-medium text-orange-200">
                    <span aria-hidden className="h-2 w-2 rounded-full bg-orange-300" />
                    {t("tk.needs.title")}
                  </h3>
                  <ApprovalCard
                    a={{ id: a.id, action: a.action, why, at: a.created_at, requested_by_name: a.requested_by_name, details }}
                    autoFocus
                    onDone={load}
                  />
                </section>
              ) : (
                <p className="text-sm text-ink-2">
                  {label("appr.status", a.status)}
                  {a.decided_at ? ` · ${ago(a.decided_at)}` : ""}
                  {a.comment ? ` · „${a.comment}“` : ""}
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ the host (once, in the Shell) */

/** Opens the panel for `?task=`, `/tasks/T-123` or `?approval=` over whatever page is showing. */
export default function TaskSheetHost() {
  const loc = useLocation();
  const navigate = useNavigate();
  const order = useSheetOrder();
  const path = loc.pathname.match(/^\/tasks\/(T-\d+)$/i);
  const q = new URLSearchParams(loc.search);
  const legacy = loc.pathname === "/tasks" ? q.get("task") : null;
  const ref = path ? path[1].toUpperCase() : legacy ? null : q.get("task")?.toUpperCase() || null;
  const approval = Number(q.get("approval")) || null;
  const focus = q.get("focus");

  // Old links (/tasks?view=…&task=T-1) become /tasks/T-1?view=….
  useEffect(() => {
    if (legacy) navigate(taskHref({ pathname: "/tasks", search: loc.search }, legacy.toUpperCase(), (focus as "needs") || undefined), { replace: true });
  }, [legacy, loc.search, focus, navigate]);

  const close = useCallback(() => {
    const p = withoutSheet(loc.search).toString();
    navigate({ pathname: path ? "/tasks" : loc.pathname, search: p ? `?${p}` : "" });
  }, [loc.pathname, loc.search, path, navigate]);

  const pos = ref ? order.indexOf(ref) : -1;
  const step = useCallback(
    (d: -1 | 1) => {
      if (pos < 0) return;
      const next = order[pos + d];
      if (next) navigate(taskHref(loc, next), { replace: true });
    },
    [order, pos, loc, navigate],
  );

  if (ref)
    return (
      <Sheet labelledBy="tk-title" onClose={close} onStep={step}>
        <TaskPanel taskRef={ref} focus={focus} initialTab={q.get("part")} onClose={close} onStep={step} pos={pos} total={order.length} />
      </Sheet>
    );
  if (approval)
    return (
      <Sheet labelledBy="tk-title" onClose={close} onStep={() => undefined}>
        <ApprovalPanel id={approval} onClose={close} />
      </Sheet>
    );
  return null;
}

