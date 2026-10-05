import { Archive, Pause, Play, Plus, Zap } from "lucide-react";
import { TaskLink } from "../taskSheet";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { LOCALE, ago, fmtTime, t } from "../i18n";
import { markdownSnippet } from "../markdownText";
import { RESULT_KEYS } from "../settingsWords";
import { confirmDialog, toast } from "./overlay";
import { Panel } from "./ui";
import { useLiveReload } from "../liveStream";

export type Schedule = {
  id: number;
  name: string;
  schedule: string;
  template: { title?: string; notes?: string; topic?: string };
  visibility: "personal" | "team";
  status: "active" | "paused";
  next_run_at: string | null;
  last_run_at: string | null;
  last_result: Record<string, unknown> | null;
  last_task_ref: string | null;
  runs: number;
  created_by: number;
  created_by_name: string;
  assignee_id: number;
  assignee_name: string;
};

/** When a schedule fires next: "za 5 min", "za 2 h", "zítra 08:00", "pá 3. 10. 15:00"; "nikdy" without a date. */
export function until(iso: string | null) {
  if (!iso) return t("time.never");
  const d = new Date(iso);
  const s = (d.getTime() - Date.now()) / 1000;
  if (s < 60) return t("sched.soon");
  if (s < 3600) return t("sched.in_min", { n: Math.round(s / 60) });
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((day(d) - day(new Date())) / 86400000);
  if (days === 0) return t("sched.in_h", { n: Math.round(s / 3600) });
  if (days === 1) return t("sched.tomorrow", { time: fmtTime(iso) });
  return d.toLocaleString(LOCALE, { weekday: "short", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function resultText(r: Record<string, unknown> | null) {
  if (!r) return "—";
  if (r.error) return t("sched.error", { e: String(r.error) });
  if (r.skipped) return String(r.skipped);
  if (r.deferred) return t("sched.deferred", { d: String(r.deferred) });
  if (r.task) return t("sched.created", { task: String(r.task) });
  return (
    Object.entries(r)
      .filter(([, v]) => v !== null && !(Array.isArray(v) && v.length === 0) && typeof v !== "object")
      .map(([k, v]) => `${RESULT_KEYS[k] ?? k.replace(/_/g, " ")}: ${v === true ? "ano" : v === false ? "ne" : (RESULT_KEYS[String(v)] ?? v)}`)
      .join(" · ") || t("sched.ok")
  );
}

/** Amber outline pill for a paused routine or schedule. */
export function PausedBadge() {
  return (
    <span className="inline-flex shrink-0 items-center rounded-full border border-amber-400/60 px-2 py-px text-xs text-amber-300">
      {t("status.paused_badge")}
    </span>
  );
}

const COLS = "md:grid-cols-[minmax(0,1.4fr)_110px_minmax(0,1fr)_100px_minmax(0,1.2fr)_96px]";
const iconBtn = "rounded p-1 text-ink-2 hover:text-accent";

function Rows({ items, onChange, onError }: { items: Schedule[]; onChange: () => void; onError: (m: string | null) => void }) {
  const act = (p: Promise<unknown>, done?: string) =>
    p.then(
      () => {
        onError(null);
        if (done) toast(done);
        onChange();
      },
      (e) => onError(e.message),
    );
  if (items.length === 0) return <p className="px-4 py-3 text-xs text-ink-2">{t("sched.none")}</p>;
  const archive = (s: Schedule) =>
    confirmDialog({
      title: t("sched.archive_title", { name: s.name }),
      body: t("sched.archive_body"),
      confirm: t("act.archive"),
      danger: true,
    }).then((ok) => {
      if (ok !== null) act(api(`/api/schedules/${s.id}/archive`, { method: "POST" }), t("sched.archived"));
    });
  return (
    <>
      <div className={`hidden ${COLS} gap-3 border-b border-line px-4 py-2 md:grid`}>
        {[t("sched.col_schedule"), t("sched.col_when"), t("sched.col_owner"), t("auto.col_next"), t("auto.col_last")].map((h) => (
          <span key={h} className="text-xs text-ink-2">
            {h}
          </span>
        ))}
        <span className="sr-only">{t("auto.col_actions")}</span>
      </div>
      {items.map((s) => {
        const active = s.status === "active";
        return (
          <div key={s.id} className={`grid grid-cols-1 gap-1.5 border-b border-line px-4 py-2.5 text-sm ${COLS} md:items-center md:gap-3`}>
            <span className="flex min-w-0 flex-col">
              <span className="flex min-w-0 items-center gap-2">
                <span className="truncate">{s.name}</span>
                {!active && <PausedBadge />}
              </span>
              {s.template.notes && <span className="truncate text-xs text-ink-2">{markdownSnippet(s.template.notes, 140)}</span>}
            </span>
            <span className="font-mono text-xs">{s.schedule}</span>
            <span className="min-w-0 truncate text-xs text-ink-2">
              <Link to={`/agents/${s.created_by}`} className="hover:text-accent">
                {s.created_by_name}
              </Link>
              {s.assignee_id !== s.created_by && (
                <>
                  {" → "}
                  <Link to={`/agents/${s.assignee_id}`} className="hover:text-accent">
                    {s.assignee_name}
                  </Link>
                </>
              )}
            </span>
            <span className="text-xs text-ink-2">{active ? until(s.next_run_at) : <PausedBadge />}</span>
            <span
              className={`min-w-0 truncate text-xs ${s.last_result?.error ? "text-red-400" : "text-ink-2"}`}
              title={s.last_run_at ? t("sched.ran", { ago: ago(s.last_run_at), n: s.runs }) : t("auto.never_ran")}
            >
              {s.last_task_ref ? (
                <TaskLink taskRef={s.last_task_ref} className="hover:text-accent">
                  {resultText(s.last_result)}
                </TaskLink>
              ) : (
                resultText(s.last_result)
              )}
            </span>
            <span className="flex justify-end gap-1.5">
              <button
                className={iconBtn}
                title={t("auto.run_now")}
                aria-label={t("auto.run_now_aria", { name: s.name })}
                onClick={() => act(api(`/api/schedules/${s.id}/run`, { method: "POST" }))}
              >
                <Zap size={14} />
              </button>
              <button
                className={iconBtn}
                title={active ? t("act.pause") : t("act.resume")}
                aria-label={t(active ? "auto.pause_aria" : "auto.resume_aria", { name: s.name })}
                onClick={() =>
                  act(api(`/api/schedules/${s.id}`, { method: "PATCH", body: JSON.stringify({ status: active ? "paused" : "active" }) }))
                }
              >
                {active ? <Pause size={14} /> : <Play size={14} />}
              </button>
              <button className={iconBtn} title={t("act.archive")} aria-label={t("auto.archive_aria", { name: s.name })} onClick={() => archive(s)}>
                <Archive size={14} />
              </button>
            </span>
          </div>
        );
      })}
    </>
  );
}

export function NewSchedule({
  assignee,
  onCreated,
  onError,
}: {
  assignee?: { id: number; name: string };
  onCreated: () => void;
  onError: (m: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ name: "", schedule: "daily 07:00", notes: "" });
  if (!open)
    return (
      <button className="btn h-7! self-start" onClick={() => setOpen(true)}>
        <Plus size={12} /> {t("sched.new")}
      </button>
    );
  const submit = () =>
    api("/api/schedules", {
      method: "POST",
      body: JSON.stringify({
        ...f,
        visibility: assignee ? "team" : "personal",
        assignee: assignee ? { type: "agent", id: assignee.id } : null,
      }),
    }).then(
      () => {
        setOpen(false);
        setF({ name: "", schedule: "daily 07:00", notes: "" });
        onError(null);
        onCreated();
      },
      (e) => onError(e.message),
    );
  const field = "h-8 rounded border border-line bg-bg px-2 text-xs outline-none focus:border-accent";
  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        value={f.name}
        onChange={(e) => setF({ ...f, name: e.target.value })}
        placeholder={assignee ? t("sched.name_ph_for", { name: assignee.name }) : t("sched.name_ph")}
        aria-label={t("sched.name_aria")}
        className={`${field} w-full min-w-0 flex-1 sm:w-auto sm:min-w-44`}
      />
      <input
        value={f.schedule}
        onChange={(e) => setF({ ...f, schedule: e.target.value })}
        aria-label={t("sched.when_aria")}
        title={t("sched.when_help")}
        className={`${field} w-32 font-mono`}
      />
      <input
        value={f.notes}
        onChange={(e) => setF({ ...f, notes: e.target.value })}
        placeholder={t("sched.notes_ph")}
        aria-label={t("sched.notes_aria")}
        className={`${field} w-full min-w-0 flex-1 sm:w-auto sm:min-w-44`}
      />
      <button className="btn h-7!" disabled={!f.name} onClick={submit}>
        {t("sched.create")}
      </button>
      <button className="text-xs text-ink-2 hover:text-accent" onClick={() => setOpen(false)}>
        {t("act.cancel")}
      </button>
    </div>
  );
}

/** Schedules of one member (split into its personal and team ones), or everyone's. */
export function SchedulesPanel({
  actor,
}: {
  actor?: { id: number; name: string };
  /** Deprecated: the decorative labels are gone; kept so old call sites compile. */
  fig?: string;
}) {
  const [items, setItems] = useState<Schedule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<Schedule[]>(actor ? `/api/schedules?actor_id=${actor.id}` : "/api/schedules").then(setItems);
  }, [actor]);
  useEffect(() => {
    load();
  }, [load]);
  useLiveReload(["schedule", "run"], load, { safetyMs: 60_000 });
  const personal = items.filter((s) => s.visibility === "personal");
  const team = items.filter((s) => s.visibility === "team");
  const header = (
    <div className="flex flex-col gap-2 border-b border-line px-4 py-2.5">
      <NewSchedule assignee={actor} onCreated={load} onError={setError} />
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );
  if (!actor)
    return (
      <Panel title={t("sched.all")} right={t("sched.all_right")}>
        {header}
        <Rows items={items} onChange={load} onError={setError} />
      </Panel>
    );
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Panel title={t("sched.personal")} right={t("sched.personal_right", { name: actor.name })}>
        <Rows items={personal} onChange={load} onError={setError} />
      </Panel>
      <Panel title={t("sched.team")} right={t("sched.team_right")}>
        {header}
        <Rows items={team} onChange={load} onError={setError} />
      </Panel>
    </div>
  );
}
