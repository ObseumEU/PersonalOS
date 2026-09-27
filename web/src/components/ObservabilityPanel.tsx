import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Panel } from "./ui";

type Alert = {
  alertname: string;
  severity: string | null;
  kind: string | null;
  host: string | null;
  target: string | null;
  summary: string | null;
  status: string;
  starts_at: string;
  received_at: string;
  resolved_at: string | null;
  dashboard_url: string | null;
  incident_id: string;
  task_ref: string | null;
};
type ObsStatus = {
  firing: Alert[];
  resolved: Alert[];
  last_webhook: { at: string; alerts: number } | null;
  grafana: { ok: boolean; fails: number; checked_at: string; since?: string } | null;
  grafana_url: string;
  configured: boolean;
  dashboards: { title: string; url: string }[];
};

const SEV: Record<string, string> = { critical: "text-red-400", high: "text-amber-300", medium: "text-ink-2", low: "text-ink-3" };
const when = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" }) : "–";

/** Systém: Grafana alerts as PersonalOS stored them (pos.observability) — no call to Grafana on a page view. */
export default function ObservabilityPanel() {
  const [st, setSt] = useState<ObsStatus | null>(null);
  useEffect(() => {
    const load = () => api<ObsStatus>("/api/observability/status").then(setSt, () => setSt(null));
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, []);
  if (!st) return null;
  const down = st.grafana && !st.grafana.ok;
  const right = !st.configured
    ? "not connected (POS_GRAFANA_TOKEN)"
    : `${st.firing.length} firing · last alert ${st.last_webhook ? when(st.last_webhook.at) : "never"}`;
  return (
    <Panel title="Systém · Grafana alerts" right={right}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b border-line px-4 py-3 text-[13px]">
        <span className="flex items-center gap-2">
          <span className={`h-1.5 w-1.5 rounded-full ${down ? "bg-red-400" : st.firing.length ? "bg-amber-300" : "bg-accent"}`} />
          {down ? `Grafana not answering since ${when(st.grafana?.since ?? null)}` : st.firing.length ? "alerts firing" : "all quiet"}
        </span>
        <span className="cap ml-auto flex flex-wrap gap-3">
          {st.dashboards.map((d) => (
            <a key={d.url} href={d.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
              {d.title} ↗
            </a>
          ))}
        </span>
      </div>
      {st.firing.length === 0 && <p className="px-4 py-3 text-xs text-ink-2">No alert is firing.</p>}
      {st.firing.map((a) => (
        <div
          key={a.incident_id}
          className="grid grid-cols-[70px_minmax(0,1fr)_90px_120px] items-center gap-3 border-b border-line px-4 py-2 text-[13px] max-sm:grid-cols-[60px_minmax(0,1fr)]"
        >
          <span className={`cap ${SEV[a.severity ?? ""] ?? ""}!`}>{a.severity ?? "?"}</span>
          <span className="truncate" title={a.summary ?? a.alertname}>
            {a.dashboard_url ? (
              <a href={a.dashboard_url} target="_blank" rel="noreferrer" className="hover:underline">
                {a.summary ?? a.alertname}
              </a>
            ) : (
              (a.summary ?? a.alertname)
            )}
          </span>
          <span className="max-sm:hidden">
            {a.task_ref && (
              <Link to={`/tasks?view=agents&task=${a.task_ref}`} className="font-mono text-xs text-accent">
                {a.task_ref}
              </Link>
            )}
          </span>
          <span className="cap text-right max-sm:hidden">since {when(a.starts_at)}</span>
        </div>
      ))}
      {st.resolved.length > 0 && (
        <p className="px-4 py-2 text-xs text-ink-3">
          Recently resolved:{" "}
          {st.resolved.map((a, i) => (
            <span key={a.incident_id}>
              {i > 0 && " · "}
              {a.alertname}
              {a.target ? ` (${a.target})` : ""} {when(a.resolved_at)}
            </span>
          ))}
        </p>
      )}
    </Panel>
  );
}
