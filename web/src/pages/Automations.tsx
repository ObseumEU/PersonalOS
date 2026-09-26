import { Archive, Pause, Play, Zap } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { markdownSnippet } from "../markdownText";
import { NewSchedule, type Schedule, resultText, until } from "../components/Schedules";
import { PageHeader, Panel } from "../components/ui";
import { ago } from "./Agents";

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
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, [load]);
  const patch = (path: string, body: unknown) => api(path, { method: "PATCH", body: JSON.stringify(body) });
  const post = (path: string) => api(path, { method: "POST" });
  // One list: the platform's jobs (owner: System) and every member's schedules.
  const routines: Routine[] = [
    ...jobs.map((j) => ({
      key: `job-${j.id}`, name: j.name, sub: j.action, owner: "System", ownerId: null, schedule: j.schedule,
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
  const run = (p: Promise<unknown>) => p.then(() => { setError(null); load(); }, (e) => setError(e.message));

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="AUTOMATIONS · SCHEDULER · A2A"
        title="Automations"
        sub="Recurring system work runs here until Nexus takes it over through its A2A facade. Jobs are plain code; when thinking is needed they hand a task to an agent."
      />
      {error && <p className="cap text-red-400!">{error}</p>}
      <Panel fig="TAB. 16" title="Routines" right="system jobs and members' schedules · Europe/Prague · the kill switch pauses all but the morning brief, weekly review and budget check">
        <div className="flex flex-col gap-2 border-b border-line px-4 py-2.5">
          <NewSchedule onCreated={load} onError={setError} />
        </div>
        <div className="hidden grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_130px_80px_minmax(0,1.3fr)_96px] gap-3 border-b border-line px-4 py-2 md:grid">
          {["ROUTINE", "OWNER", "WHEN", "NEXT", "LAST RESULT", ""].map((h) => (
            <span key={h} className="cap">
              {h}
            </span>
          ))}
        </div>
        {routines.map((r) => (
          <div
            key={r.key}
            className={`grid grid-cols-1 gap-1.5 border-b border-line px-4 py-2.5 text-[13px] md:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)_130px_80px_minmax(0,1.3fr)_96px] md:items-center md:gap-3 ${r.active ? "" : "opacity-45"}`}
          >
            <span className="flex min-w-0 flex-col">
              <span className="truncate">{r.name}</span>
              {r.sub && <span className="cap truncate">{r.sub}</span>}
            </span>
            <span className="cap truncate">
              {r.ownerId ? (
                <Link to={`/team/${r.ownerId}`} className="hover:text-accent!">
                  {r.owner}
                </Link>
              ) : (
                r.owner
              )}
            </span>
            <input
              defaultValue={r.schedule}
              aria-label={`When ${r.name} runs`}
              onBlur={(e) => e.target.value !== r.schedule && run(r.setSchedule(e.target.value))}
              className="h-7 rounded border border-line bg-bg px-2 font-mono text-xs outline-none focus:border-accent"
            />
            <span className="cap">{r.active ? until(r.next) : "paused"}</span>
            <span className={`cap truncate ${r.error ? "text-red-400!" : ""}`} title={r.last ? `ran ${ago(r.last)}` : "never ran"}>
              {r.lastRef ? (
                <Link to={`/tasks?task=${r.lastRef}`} className="hover:text-accent!">
                  {r.lastText}
                </Link>
              ) : (
                r.lastText
              )}
            </span>
            <span className="flex justify-end gap-2.5">
              <button className="cap hover:text-accent!" title="Run now" aria-label={`Run ${r.name} now`} onClick={() => run(r.fire())}>
                <Zap size={13} />
              </button>
              <button className="cap hover:text-accent!" title={r.active ? "Pause" : "Resume"} aria-label={`${r.active ? "Pause" : "Resume"} ${r.name}`} onClick={() => run(r.toggle())}>
                {r.active ? <Pause size={13} /> : <Play size={13} />}
              </button>
              {r.archive && (
                <button className="cap hover:text-accent!" title="Archive" aria-label={`Archive ${r.name}`} onClick={() => run(r.archive!())}>
                  <Archive size={13} />
                </button>
              )}
            </span>
          </div>
        ))}
      </Panel>

      <div className="grid gap-4 lg:grid-cols-12">
        <Panel fig="A2A" title="Remote agents" right="tasks assigned to them travel over A2A" className="lg:col-span-5">
          {a2a?.members.length === 0 && (
            <p className="px-4 py-3 text-xs leading-relaxed text-ink-2">
              None connected yet. Set <span className="font-mono">POS_KNOWLAGE_A2A_URL</span> (knowlage-agent) or{" "}
              <span className="font-mono">POS_NEXUS_A2A_URL</span> with keys in <span className="font-mono">POS_A2A_KEY_&lt;NAME&gt;</span>, or add an
              outside agent by its card on the Agents page.
            </p>
          )}
          {a2a?.members.map((m) => (
            <Link key={m.id} to={`/agents/${m.id}`} className="flex flex-col gap-0.5 border-b border-line px-4 py-2.5 hover:bg-raised">
              <span className="text-[13px]">{m.name}</span>
              <span className="cap truncate">{m.a2a_url}</span>
            </Link>
          ))}
          <p className="px-4 py-3 text-xs leading-relaxed text-ink-2">
            PersonalOS is an A2A agent too: <span className="font-mono">/.well-known/agent-card.json</span>, JSON-RPC at{" "}
            <span className="font-mono">/a2a</span> (SendMessage, GetTask, CancelTask) with an agent key.
          </p>
        </Panel>
        <Panel fig="LOG" title="Delegated over A2A" right="SendMessage → GetTask until done" className="lg:col-span-7">
          {a2a?.links.length === 0 && <p className="cap p-4">Nothing delegated yet.</p>}
          {a2a?.links.map((l) => (
            <div key={l.task_ref} className="grid grid-cols-[70px_minmax(0,1fr)_140px_90px] gap-2 border-b border-line px-4 py-2 text-[13px]">
              <Link to={`/tasks?view=agents&task=${l.task_ref}`} className="font-mono text-xs text-accent">
                {l.task_ref}
              </Link>
              <span className="truncate">
                {l.member} <span className="cap">remote {l.remote_task_id}</span>
              </span>
              <span className={`cap ${l.state === "completed" ? "text-accent!" : ""}`}>{l.state}</span>
              <span className="cap text-right">{ago(l.updated_at)}</span>
            </div>
          ))}
        </Panel>
      </div>
    </div>
  );
}
