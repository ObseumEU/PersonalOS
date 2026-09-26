import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Panel } from "./ui";

type Check = { name: string; service: string; ok: boolean; fails: number };
type OpenIncident = { id: string; service: string; kind: string; severity: string; title: string; count: number; opened_at: string };
type Incident = {
  incident_id: string;
  service: string;
  kind: string;
  severity: string;
  title: string;
  status: string;
  classification: string | null;
  llm_skipped: number;
  opened_at: string;
  task_ref: string | null;
  task_status: string | null;
};
type SentinelStatus = {
  age_s: number | null;
  stale: boolean;
  heartbeat: {
    instance: string;
    learning?: boolean;
    checks: Check[];
    open_incidents: OpenIncident[];
    counters: Record<string, number>;
    host: { swap_pct?: number; mem_avail_pct?: number; load5?: number; cpus?: number; disk_pct?: Record<string, number> };
  } | null;
  incidents: Incident[];
};

const SEV: Record<string, string> = { critical: "text-red-400", high: "text-amber-300", medium: "text-ink-2", low: "text-ink-3" };

/** The sentinel's heartbeat (pos.monitor): its checks, open incidents and what the Monitor agent did. */
export default function SentinelPanel() {
  const [st, setSt] = useState<SentinelStatus | null>(null);
  useEffect(() => {
    const load = () => api<SentinelStatus>("/api/sentinel/status").then(setSt, () => setSt(null));
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, []);
  if (!st) return null;
  const hb = st.heartbeat;
  const failing = hb?.checks.filter((c) => !c.ok) ?? [];
  const age = st.age_s === null ? "never" : st.age_s < 120 ? `${st.age_s} s ago` : `${Math.round(st.age_s / 60)} min ago`;
  const disk = hb?.host.disk_pct ? Math.max(...Object.values(hb.host.disk_pct)) : null;
  return (
    <Panel
      fig="TAB. 19"
      title="Sentinel · Hlídač"
      right={hb ? `heartbeat ${age}${hb.learning ? " · learning" : ""}` : "no sentinel yet (docker compose --profile sentinel up -d)"}
    >
      {hb && (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-b border-line px-4 py-3 text-[13px]">
          <span className="flex items-center gap-2">
            <span className={`h-1.5 w-1.5 rounded-full ${st.stale ? "bg-red-400" : failing.length ? "bg-amber-300" : "bg-accent"}`} />
            {st.stale ? "heartbeat stopped" : `${hb.checks.length - failing.length}/${hb.checks.length} checks green`}
          </span>
          {failing.length > 0 && <span className="text-amber-300">failing: {failing.map((c) => c.name).join(", ")}</span>}
          <span className="cap">
            open {hb.open_incidents.length} · 24 h: {hb.counters.incidents_24h ?? 0} incidents, {hb.counters.remediations_24h ?? 0} restarts
          </span>
          <span className="cap ml-auto">
            swap {hb.host.swap_pct ?? "?"} % · mem free {hb.host.mem_avail_pct ?? "?"} % · disk {disk ?? "?"} % · load {hb.host.load5 ?? "?"}/{hb.host.cpus ?? "?"}
          </span>
        </div>
      )}
      {st.incidents.length === 0 && <p className="px-4 py-3 text-xs text-ink-2">No incident has reached PersonalOS yet.</p>}
      {st.incidents.map((i) => (
        <div key={i.incident_id} className="grid grid-cols-[70px_minmax(0,1fr)_130px_80px_120px] items-center gap-3 border-b border-line px-4 py-2 text-[13px]">
          <span className={`cap ${SEV[i.severity] ?? ""}!`}>{i.severity}</span>
          <span className="truncate" title={i.title}>
            {i.service} · {i.kind}: {i.title}
          </span>
          <span className="cap">{i.classification ?? (i.llm_skipped ? "to owner (cap)" : i.status)}</span>
          <span>
            {i.task_ref && (
              <Link to={`/tasks?view=agents&task=${i.task_ref}`} className="font-mono text-xs text-accent">
                {i.task_ref}
              </Link>
            )}
          </span>
          <span className="cap text-right">{new Date(i.opened_at).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" })}</span>
        </div>
      ))}
    </Panel>
  );
}
