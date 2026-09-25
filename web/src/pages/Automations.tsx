import { Play } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { SchedulesPanel, resultText, until } from "../components/Schedules";
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
type Link_ = { task_ref: string; member: string; remote_task_id: string | null; state: string; updated_at: string };
type A2A = { members: { id: number; name: string; a2a_url: string }[]; links: Link_[] };

export default function Automations() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [a2a, setA2a] = useState<A2A | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<Job[]>("/api/jobs").then(setJobs);
    api<A2A>("/api/a2a/links").then(setA2a);
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, [load]);
  const run = (p: Promise<unknown>) => p.then(() => { setError(null); load(); }, (e) => setError(e.message));

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="AUTOMATIONS · SCHEDULER · A2A"
        title="Automations"
        sub="Recurring system work runs here until Nexus takes it over through its A2A facade. Jobs are plain code; when thinking is needed they hand a task to an agent."
      />
      {error && <p className="cap text-red-400!">{error}</p>}
      <Panel fig="TAB. 16" title="Scheduled jobs" right="Europe/Prague · paused by the kill switch except the brief and budget">
        <div className="grid grid-cols-[minmax(0,1.3fr)_130px_90px_90px_minmax(0,1.4fr)_120px] gap-3 border-b border-line px-4 py-2">
          {["JOB", "SCHEDULE", "NEXT", "LAST", "LAST RESULT", ""].map((h) => (
            <span key={h} className="cap">
              {h}
            </span>
          ))}
        </div>
        {jobs.map((j) => (
          <div key={j.id} className={`grid grid-cols-[minmax(0,1.3fr)_130px_90px_90px_minmax(0,1.4fr)_120px] items-center gap-3 border-b border-line px-4 py-2.5 text-[13px] ${j.enabled ? "" : "opacity-45"}`}>
            <span className="flex flex-col">
              <span>{j.name}</span>
              <span className="cap">{j.action}</span>
            </span>
            <input
              defaultValue={j.schedule}
              aria-label={`Schedule of ${j.name}`}
              onBlur={(e) => e.target.value !== j.schedule && run(api(`/api/jobs/${j.id}`, { method: "PATCH", body: JSON.stringify({ schedule: e.target.value }) }))}
              className="h-7 rounded border border-line bg-bg px-2 font-mono text-xs outline-none focus:border-accent"
            />
            <span className="cap">{j.enabled ? until(j.next_run_at) : "off"}</span>
            <span className="cap">{j.last_run_at ? ago(j.last_run_at) : "never"}</span>
            <span className={`cap truncate ${j.last_result?.error ? "text-red-400!" : ""}`}>{resultText(j.last_result)}</span>
            <span className="flex justify-end gap-2">
              <button className="btn h-7!" title="Run now" onClick={() => run(api(`/api/jobs/${j.id}/run`, { method: "POST" }))}>
                <Play size={12} /> Run
              </button>
              <button className="cap hover:text-accent!" onClick={() => run(api(`/api/jobs/${j.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !j.enabled }) }))}>
                {j.enabled ? "off" : "on"}
              </button>
            </span>
          </div>
        ))}
      </Panel>

      <SchedulesPanel fig="TAB. 17" />

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
