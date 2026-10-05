import { useEffect, useState } from "react";
import { TaskLink } from "../taskSheet";
import { api } from "../api";
import { fmtDateTime, label, plural, t } from "../i18n";
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

const SEV: Record<string, string> = { critical: "text-red-400", high: "text-amber-300", medium: "text-ink-2", low: "text-ink-2" };

/** The Monitor's classification in words (raw keys stay in the tooltip). */
const CLASS_CS: Record<string, string> = {
  resolved: "vyřešeno",
  capacity: "kapacita serveru",
  config: "nastavení",
  external_quota: "limit externí služby",
  code_bug: "chyba v kódu",
  flaky: "nestabilní",
  noise: "šum",
};

const isDone = (i: Incident) => i.status === "resolved" || i.classification === "resolved" || i.task_status === "done";

/** One row per service and kind (the newest), with how many times it came; resolved groups fold away. */
function groups(items: Incident[]): { head: Incident; n: number; done: boolean }[] {
  const by = new Map<string, Incident[]>();
  for (const i of items) {
    const k = `${i.service}|${i.kind}`;
    by.set(k, [...(by.get(k) ?? []), i]);
  }
  return [...by.values()].map((xs) => {
    const sorted = [...xs].sort((a, b) => b.opened_at.localeCompare(a.opened_at));
    return { head: sorted[0], n: xs.length, done: isDone(sorted[0]) };
  });
}

function Row({ i, n }: { i: Incident; n: number }) {
  return (
    <div className="grid grid-cols-[70px_minmax(0,1fr)] items-center gap-x-3 gap-y-1 border-b border-line px-4 py-2 text-[13px] md:grid-cols-[80px_minmax(0,1fr)_150px_80px_120px]">
      <span className={`text-xs ${SEV[i.severity] ?? "text-ink-2"}`}>{label("obs.sev", i.severity)}</span>
      <span className="truncate" title={i.title}>
        {i.service} · {i.kind}: {i.title}
        {n > 1 && <span className="ml-1.5 text-xs text-ink-2">×{n}</span>}
      </span>
      <span className="col-start-2 truncate text-xs text-ink-2 md:col-start-auto" title={i.classification ?? undefined}>
        {i.classification ? (CLASS_CS[i.classification] ?? i.classification) : i.llm_skipped ? t("sentinel.to_owner") : label("sentinel.status", i.status)}
      </span>
      <span className="col-start-2 md:col-start-auto">
        {i.task_ref && (
          <TaskLink taskRef={i.task_ref} className="font-mono text-xs text-accent">
            {i.task_ref}
          </TaskLink>
        )}
      </span>
      <span className="col-start-2 text-xs text-ink-2 md:col-start-auto md:text-right">{fmtDateTime(i.opened_at)}</span>
    </div>
  );
}

/** The sentinel's heartbeat (pos.monitor): its checks, open incidents and what the Monitor agent did. */
export default function SentinelPanel() {
  const [st, setSt] = useState<SentinelStatus | null>(null);
  useEffect(() => {
    const load = () => api<SentinelStatus>("/api/sentinel/status").then(setSt, () => setSt(null));
    load();
    const h = setInterval(load, 60_000);
    return () => clearInterval(h);
  }, []);
  if (!st) return null;
  const hb = st.heartbeat;
  const failing = hb?.checks.filter((c) => !c.ok) ?? [];
  const age = st.age_s === null ? t("time.never") : st.age_s < 120 ? t("sentinel.s_ago", { n: st.age_s }) : t("time.min_ago", { n: Math.round(st.age_s / 60) });
  const disk = hb?.host.disk_pct ? Math.max(...Object.values(hb.host.disk_pct)) : null;
  const inc24 = hb?.counters.incidents_24h ?? 0;
  const rest24 = hb?.counters.remediations_24h ?? 0;
  return (
    <Panel
      title={t("sentinel.title")}
      right={hb ? `${t("sentinel.heartbeat", { age })}${hb.learning ? t("sentinel.learning") : ""}` : t("sentinel.none")}
    >
      {hb && (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-b border-line px-4 py-3 text-[13px]">
          <span className="flex items-center gap-2">
            <span className={`h-1.5 w-1.5 rounded-full ${st.stale ? "bg-red-400" : failing.length ? "bg-amber-300" : "bg-accent"}`} />
            {st.stale ? t("sentinel.stopped") : t("sentinel.green", { ok: hb.checks.length - failing.length, n: hb.checks.length })}
          </span>
          {failing.length > 0 && <span className="break-words text-amber-300">{t("sentinel.failing", { names: failing.map((c) => c.name).join(", ") })}</span>}
          <span className="text-xs text-ink-2">
            {t("sentinel.counts", {
              open: hb.open_incidents.length,
              inc: inc24,
              incidents: plural(inc24, "incident", "incidenty", "incidentů"),
              rest: rest24,
              restarts: plural(rest24, "restart", "restarty", "restartů"),
            })}
          </span>
          <span className="text-xs text-ink-2 sm:ml-auto">
            {t("sentinel.host", {
              swap: hb.host.swap_pct ?? "?",
              mem: hb.host.mem_avail_pct ?? "?",
              disk: disk ?? "?",
              load: hb.host.load5 ?? "?",
              cpus: hb.host.cpus ?? "?",
            })}
          </span>
        </div>
      )}
      {st.incidents.length === 0 && <p className="px-4 py-3 text-xs text-ink-2">{t("sentinel.no_incidents")}</p>}
      {groups(st.incidents)
        .filter((g) => !g.done)
        .map((g) => (
          <Row key={g.head.incident_id} i={g.head} n={g.n} />
        ))}
      {groups(st.incidents).some((g) => g.done) && (
        <details className="border-b border-line">
          <summary className="cursor-pointer px-4 py-2 text-xs text-ink-2">
            {t("sentinel.resolved_folded", { n: groups(st.incidents).filter((g) => g.done).length })}
          </summary>
          {groups(st.incidents)
            .filter((g) => g.done)
            .map((g) => (
              <Row key={g.head.incident_id} i={g.head} n={g.n} />
            ))}
        </details>
      )}
    </Panel>
  );
}
