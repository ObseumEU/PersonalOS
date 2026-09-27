import { useEffect, useState } from "react";
import { TaskLink } from "../taskSheet";
import { api } from "../api";
import { LOCALE, label, t } from "../i18n";
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

const SEV: Record<string, string> = { critical: "text-red-400", high: "text-amber-300", medium: "text-ink-2", low: "text-ink-2" };
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" }) : "–");

/** Systém: Grafana alerts as PersonalOS stored them (pos.observability) — no call to Grafana on a page view. */
export default function ObservabilityPanel() {
  const [st, setSt] = useState<ObsStatus | null>(null);
  useEffect(() => {
    const load = () => api<ObsStatus>("/api/observability/status").then(setSt, () => setSt(null));
    load();
    const h = setInterval(load, 60_000);
    return () => clearInterval(h);
  }, []);
  if (!st) return null;
  const down = st.grafana && !st.grafana.ok;
  const right = !st.configured
    ? t("obs.not_connected")
    : t("obs.right", { n: st.firing.length, last: st.last_webhook ? when(st.last_webhook.at) : t("time.never") });
  return (
    <Panel title={t("obs.title")} right={right}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b border-line px-4 py-3 text-[13px]">
        <span className="flex items-center gap-2">
          <span className={`h-1.5 w-1.5 rounded-full ${down ? "bg-red-400" : st.firing.length ? "bg-amber-300" : "bg-accent"}`} />
          {down ? t("obs.down", { since: when(st.grafana?.since ?? null) }) : st.firing.length ? t("obs.firing") : t("obs.quiet")}
        </span>
        <span className="ml-auto flex flex-wrap gap-3 text-xs">
          {st.dashboards.map((d) => (
            <a key={d.url} href={d.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
              {d.title} ↗
            </a>
          ))}
        </span>
      </div>
      {st.firing.length === 0 && <p className="px-4 py-3 text-xs text-ink-2">{t("obs.none")}</p>}
      {st.firing.map((a) => (
        <div
          key={a.incident_id}
          className="grid grid-cols-[70px_minmax(0,1fr)] items-center gap-3 border-b border-line px-4 py-2 text-[13px] sm:grid-cols-[80px_minmax(0,1fr)_90px_140px]"
        >
          <span className={`text-xs ${SEV[a.severity ?? ""] ?? "text-ink-2"}`}>{a.severity ? label("obs.sev", a.severity) : "?"}</span>
          <span className="truncate" title={a.summary ?? a.alertname}>
            {a.dashboard_url ? (
              <a href={a.dashboard_url} target="_blank" rel="noreferrer" className="hover:underline">
                {a.summary ?? a.alertname}
              </a>
            ) : (
              (a.summary ?? a.alertname)
            )}
          </span>
          <span className="hidden sm:inline">
            {a.task_ref && (
              <TaskLink taskRef={a.task_ref} className="font-mono text-xs text-accent">
                {a.task_ref}
              </TaskLink>
            )}
          </span>
          <span className="hidden text-right text-xs text-ink-2 sm:inline">{t("obs.since", { when: when(a.starts_at) })}</span>
        </div>
      ))}
      {st.resolved.length > 0 && (
        <p className="px-4 py-2 text-xs break-words text-ink-2">
          {t("obs.resolved")}{" "}
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
