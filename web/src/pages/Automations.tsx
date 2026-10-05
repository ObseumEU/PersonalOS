import { Archive, Pause, Pencil, Play, Zap } from "lucide-react";
import { TaskLink } from "../taskSheet";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { confirmDialog, toast } from "../components/overlay";
import { NewSchedule, PausedBadge, type Schedule, resultText, until } from "../components/Schedules";
import { PageHeader, Panel } from "../components/ui";
import { ago, label, t } from "../i18n";
import { markdownSnippet } from "../markdownText";
import { useLiveReload } from "../liveStream";
import { jobName, scheduleCs } from "../settingsWords";

type Job = {
  id: number;
  name: string;
  schedule: string;
  action: string;
  enabled: boolean;
  next_run_at: string;
  last_run_at: string | null;
  last_result: Record<string, unknown> | null;
};
type Routine = {
  key: string;
  name: string;
  sub: string;
  /** The routine's key (morning_brief): only in the tooltip. */
  tech?: string;
  owner: string;
  ownerId: number | null;
  schedule: string;
  active: boolean;
  next: string | null;
  last: string | null;
  lastText: string;
  lastRef: string | null;
  error: boolean;
  fire: () => Promise<unknown>;
  toggle: () => Promise<unknown>;
  setSchedule: (v: string) => Promise<unknown>;
  archive: (() => Promise<unknown>) | null;
};
type Link_ = { task_ref: string; member: string; remote_task_id: string | null; state: string; updated_at: string };
type A2A = { members: { id: number; name: string; a2a_url: string }[]; links: Link_[] };

const COLS = "md:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_minmax(0,170px)_100px_minmax(0,1.3fr)_112px]";
const iconBtn = "rounded p-1 text-ink-2 hover:text-accent";

export default function Automations() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [a2a, setA2a] = useState<A2A | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<Job[]>("/api/jobs").then(setJobs);
    api<Schedule[]>("/api/schedules").then(setSchedules);
    api<A2A>("/api/a2a/links").then(setA2a);
  }, []);
  useEffect(() => {
    load();
  }, [load]);
  // Runs of jobs and schedules write no audit entry: the safety reload keeps "last run" fresh.
  useLiveReload(["job", "schedule", "route", "run"], load, { safetyMs: 60_000 });
  const patch = (path: string, body: unknown) => api(path, { method: "PATCH", body: JSON.stringify(body) });
  const post = (path: string) => api(path, { method: "POST" });
  // One list: the platform's jobs (owner: System) and every member's schedules.
  const routines: Routine[] = [
    ...jobs.map((j) => ({
      key: `job-${j.id}`, name: jobName(j.action, j.name), sub: "", tech: j.action, owner: t("auto.system"), ownerId: null, schedule: j.schedule,
      active: j.enabled, next: j.next_run_at, last: j.last_run_at, lastText: resultText(j.last_result),
      lastRef: null, error: Boolean(j.last_result?.error),
      fire: () => post(`/api/jobs/${j.id}/run`), toggle: () => patch(`/api/jobs/${j.id}`, { enabled: !j.enabled }),
      setSchedule: (v: string) => patch(`/api/jobs/${j.id}`, { schedule: v }), archive: null,
    })),
    ...schedules.map((x) => ({
      key: `sch-${x.id}`, name: x.name, sub: markdownSnippet(x.template.notes, 140),
      owner: x.assignee_id === x.created_by ? x.created_by_name : `${x.created_by_name} → ${x.assignee_name}`,
      ownerId: x.assignee_id, schedule: x.schedule, active: x.status === "active", next: x.next_run_at,
      last: x.last_run_at, lastText: resultText(x.last_result), lastRef: x.last_task_ref, error: Boolean(x.last_result?.error),
      fire: () => post(`/api/schedules/${x.id}/run`),
      toggle: () => patch(`/api/schedules/${x.id}`, { status: x.status === "active" ? "paused" : "active" }),
      setSchedule: (v: string) => patch(`/api/schedules/${x.id}`, { schedule: v }),
      archive: () => post(`/api/schedules/${x.id}/archive`),
    })),
  ];
  const run = (p: Promise<unknown>) =>
    p.then(
      () => {
        setError(null);
        load();
        return true;
      },
      (e) => {
        setError(e.message);
        return false;
      },
    );

  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={t("auto.kicker")} title={t("auto.title")} sub={t("auto.sub")} />
      {error && <p className="text-sm text-red-400">{error}</p>}
      <Panel title={t("auto.routines")} right={t("auto.routines_right")}>
        <div className="flex flex-col gap-2 border-b border-line px-4 py-2.5">
          <NewSchedule onCreated={load} onError={setError} />
        </div>
        <div className={`hidden ${COLS} gap-3 border-b border-line px-4 py-2 md:grid`}>
          {[t("auto.col_routine"), t("auto.col_owner"), t("auto.col_when"), t("auto.col_next"), t("auto.col_last")].map((h) => (
            <span key={h} className="text-xs text-ink-2">
              {h}
            </span>
          ))}
          <span className="sr-only">{t("auto.col_actions")}</span>
        </div>
        {routines.map((r) => (
          <RoutineRow key={r.key} r={r} run={run} load={load} onError={setError} />
        ))}
      </Panel>

      <div className="grid gap-4 lg:grid-cols-12">
        <Panel title={t("auto.remote")} right={t("auto.remote_right")} className="lg:col-span-5">
          {a2a?.members.length === 0 && (
            <p className="px-4 py-3 text-xs leading-relaxed text-ink-2">
              {t("auto.remote_none_1")} <code className="font-mono">POS_KNOWLAGE_A2A_URL</code> {t("auto.remote_none_2")}{" "}
              <code className="font-mono">POS_NEXUS_A2A_URL</code> {t("auto.remote_none_3")}{" "}
              <code className="font-mono break-all">POS_A2A_KEY_&lt;NAME&gt;</code>
              {t("auto.remote_none_4")}
            </p>
          )}
          {a2a?.members.map((m) => (
            <Link key={m.id} to={`/agents/${m.id}`} className="flex min-w-0 flex-col gap-0.5 border-b border-line px-4 py-2.5 hover:bg-raised">
              <span className="text-sm">{m.name}</span>
              <span className="truncate font-mono text-xs text-ink-2">{m.a2a_url}</span>
            </Link>
          ))}
          <p className="px-4 py-3 text-xs leading-relaxed text-ink-2">
            {t("auto.self_1")} <code className="font-mono break-all">/.well-known/agent-card.json</code>
            {t("auto.self_2")} <code className="font-mono">/a2a</code> {t("auto.self_3")}
          </p>
        </Panel>
        <Panel title={t("auto.delegated")} right={t("auto.delegated_right")} className="lg:col-span-7">
          {a2a?.links.length === 0 && <p className="p-4 text-xs text-ink-2">{t("auto.delegated_none")}</p>}
          {a2a?.links.map((l) => (
            <div
              key={l.task_ref}
              className="grid grid-cols-1 gap-1 border-b border-line px-4 py-2 text-sm md:grid-cols-[70px_minmax(0,1fr)_140px_90px] md:gap-2"
            >
              <TaskLink taskRef={l.task_ref} className="font-mono text-xs text-accent">
                {l.task_ref}
              </TaskLink>
              <span className="min-w-0 truncate">
                {l.member}{" "}
                {l.remote_task_id && <span className="text-xs text-ink-2">{t("auto.remote_id", { id: l.remote_task_id })}</span>}
              </span>
              <span className={`text-xs ${l.state === "completed" ? "text-accent" : "text-ink-2"}`}>{label("auto.state", l.state)}</span>
              <span className="text-xs text-ink-2 md:text-right">{ago(l.updated_at)}</span>
            </div>
          ))}
        </Panel>
      </div>
    </div>
  );
}

/** One routine: the schedule is plain text until "Upravit"; then an input with Uložit / Zrušit. */
function RoutineRow({
  r,
  run,
  load,
  onError,
}: {
  r: Routine;
  run: (p: Promise<unknown>) => Promise<boolean>;
  load: () => void;
  onError: (m: string | null) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(r.schedule);
  const [saving, setSaving] = useState(false);
  const cancel = () => {
    setValue(r.schedule);
    setEditing(false);
  };
  const save = () => {
    const previous = r.schedule;
    const next = value.trim();
    if (!next || next === previous) return cancel();
    setSaving(true);
    r.setSchedule(next).then(
      () => {
        setSaving(false);
        setEditing(false);
        onError(null);
        load();
        toast(t("auto.schedule_saved"), { undo: () => r.setSchedule(previous).then(load) });
      },
      (e) => {
        setSaving(false);
        onError(e.message);
      },
    );
  };
  const archive = () =>
    confirmDialog({
      title: t("auto.archive_title", { name: r.name }),
      body: t("auto.archive_body"),
      confirm: t("act.archive"),
      danger: true,
    }).then((ok) => {
      if (ok !== null && r.archive) run(r.archive()).then((done) => done && toast(t("auto.archived")));
    });
  return (
    <div className={`grid grid-cols-1 gap-1.5 border-b border-line px-4 py-2.5 text-sm ${COLS} md:items-center md:gap-3`}>
      <span className="flex min-w-0 flex-col">
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate" title={r.tech}>
            {r.name}
          </span>
          {!r.active && <PausedBadge />}
        </span>
        {r.sub && <span className="truncate text-xs text-ink-2">{r.sub}</span>}
      </span>
      <span className="min-w-0 truncate text-xs text-ink-2">
        {r.ownerId ? (
          <Link to={`/team/${r.ownerId}`} className="hover:text-accent">
            {r.owner}
          </Link>
        ) : (
          r.owner
        )}
      </span>
      {editing ? (
        <span className="flex min-w-0 flex-wrap items-center gap-1.5">
          <input
            autoFocus
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") save();
              if (e.key === "Escape") cancel();
            }}
            aria-label={t("auto.when_aria", { name: r.name })}
            title={t("sched.when_help")}
            className="h-7 w-full min-w-0 rounded border border-line bg-bg px-2 font-mono text-xs outline-none focus:border-accent"
          />
          <button className="btn h-7!" disabled={saving || !value.trim()} onClick={save}>
            {t("act.save")}
          </button>
          <button className="text-xs text-ink-2 hover:text-accent" onClick={cancel}>
            {t("act.cancel")}
          </button>
        </span>
      ) : (
        <span className="min-w-0 truncate text-xs" title={r.schedule}>
          {scheduleCs(r.schedule)}
        </span>
      )}
      <span className="text-xs text-ink-2">{r.active ? until(r.next) : <PausedBadge />}</span>
      <span
        className={`min-w-0 truncate text-xs ${r.error ? "text-red-400" : "text-ink-2"}`}
        title={r.last ? t("auto.ran", { ago: ago(r.last) }) : t("auto.never_ran")}
      >
        {r.lastRef ? (
          <TaskLink taskRef={r.lastRef} className="hover:text-accent">
            {r.lastText}
          </TaskLink>
        ) : (
          r.lastText
        )}
      </span>
      <span className="flex justify-end gap-1.5">
        <button
          className={iconBtn}
          title={t("act.edit")}
          aria-label={t("auto.edit_aria", { name: r.name })}
          onClick={() => {
            setValue(r.schedule);
            setEditing(true);
          }}
        >
          <Pencil size={14} />
        </button>
        <button className={iconBtn} title={t("auto.run_now")} aria-label={t("auto.run_now_aria", { name: r.name })} onClick={() => run(r.fire())}>
          <Zap size={14} />
        </button>
        <button
          className={iconBtn}
          title={r.active ? t("act.pause") : t("act.resume")}
          aria-label={t(r.active ? "auto.pause_aria" : "auto.resume_aria", { name: r.name })}
          onClick={() => run(r.toggle())}
        >
          {r.active ? <Pause size={14} /> : <Play size={14} />}
        </button>
        {r.archive && (
          <button className={iconBtn} title={t("act.archive")} aria-label={t("auto.archive_aria", { name: r.name })} onClick={archive}>
            <Archive size={14} />
          </button>
        )}
      </span>
    </div>
  );
}
