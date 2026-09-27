import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { type Engines, agentsApi } from "../agentsApi";
import BudgetPanel from "../components/BudgetPanel";
import ObservabilityPanel from "../components/ObservabilityPanel";
import SentinelPanel from "../components/SentinelPanel";
import { PageHeader, Panel } from "../components/ui";
import { LOCALE, fmtDateTime, label, plural, t } from "../i18n";
import { useSubsystems } from "../knowledgeApi";

type Deploy = {
  id: number;
  old_sha: string;
  new_sha: string;
  status: "ok" | "reverted" | "rejected" | "error";
  stage: string;
  author: string;
  reverted_sha: string | null;
  commits: number;
  task_ref: string | null;
  created_at: string;
  log: string;
};

const DEPLOY_COLOR: Record<Deploy["status"], string> = {
  ok: "text-accent",
  reverted: "text-amber-300",
  rejected: "text-amber-300",
  error: "text-red-400",
};

/** Subsystem names come from the API in English; known ones get a Czech label. */
const SUBSYSTEM: Record<string, string> = {
  "Knowledge base": "misc.knowlage",
  Nexus: "sys.sub.nexus",
  "Agent runtime": "sys.sub.runtime",
};
const subName = (name: string) => (SUBSYSTEM[name] ? t(SUBSYSTEM[name]) : name);

/** The API's detail line in Czech where the pattern is known; the raw text otherwise. */
function subDetail(detail: string): string {
  const docs = detail.match(/^([\d\s]+) documents · ([\d\s]+) chunks$/);
  if (docs) return t("sys.sub.docs", { docs: docs[1].trim(), chunks: docs[2].trim() });
  if (detail === "unreachable" || detail === "no runtime available") return t(detail === "unreachable" ? "sys.sub.unreachable" : "sys.sub.no_runtime");
  if (detail === "reachable") return t("sys.sub.reachable");
  const notReach = detail.match(/^not reachable \((.*)\)$/);
  if (notReach) return t("sys.sub.not_reachable", { why: notReach[1] });
  if (detail.startsWith("not connected yet")) return t("sys.sub.not_connected", { env: detail.match(/\((.*)\)/)?.[1] ?? "" });
  const now = detail.match(/^(\w+) now(?: · (\w+) limited until (.+))?$/);
  if (now) return now[2] ? t("sys.sub.runtime_now_limited", { now: now[1], other: now[2], until: now[3] }) : t("sys.sub.runtime_now", { now: now[1] });
  return detail;
}

const when = (iso: string) => new Date(iso).toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" });

export default function System() {
  const [api_, setApi] = useState<{ version: string; phase: string } | null>(null);
  const [deploys, setDeploys] = useState<Deploy[]>([]);
  const [engines, setEngines] = useState<Engines | null>(null);
  const subsystems = useSubsystems();
  useEffect(() => {
    api<{ version: string; phase: string }>("/api/system").then(setApi, () => setApi(null));
    api<Deploy[]>("/api/deploys").then(setDeploys, () => setDeploys([]));
    agentsApi.engines().then(setEngines, () => setEngines(null));
  }, []);

  return (
    <div className="flex min-w-0 flex-col gap-5">
      <PageHeader
        kicker={t("settings.kicker")}
        title={t("sys.title")}
        sub={api_ ? t("sys.sub", { version: api_.version, phase: api_.phase }) : t("sys.sub_down")}
      />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {subsystems?.map((s) => (
          <Panel key={s.name} bodyClassName="flex min-w-0 flex-col gap-2.5 px-4 py-3.5">
            <span className="flex min-w-0 items-baseline gap-2.5">
              <span className={`h-1.5 w-1.5 shrink-0 self-center rounded-full ${s.ok ? "bg-accent" : "bg-ink-3"}`} />
              <span className="min-w-0 truncate text-sm font-medium">{subName(s.name)}</span>
              <span className="ml-auto shrink-0 text-xs text-ink-2">{s.proto}</span>
            </span>
            <span className="flex items-baseline gap-2">
              <span className="font-mono text-[26px]">{s.value ?? (s.ok ? "ok" : "—")}</span>
              <span className="text-xs text-ink-2">{s.value !== null ? t("sys.sub.health", { unit: s.unit }) : ""}</span>
            </span>
            <span className="min-w-0 text-xs break-words text-ink-2">
              {s.url ? (
                <a href={s.url} target="_blank" rel="noreferrer" className="hover:text-accent">
                  {subDetail(s.detail)} ↗
                </a>
              ) : (
                subDetail(s.detail)
              )}
            </span>
          </Panel>
        ))}
      </div>
      {engines && (
        <Panel title={t("sys.runtimes")} right={t("sys.runtimes_right", { engine: label("engine", engines.default) })}>
          <div className="grid grid-cols-1 md:grid-cols-2">
            <div className="flex min-w-0 flex-col gap-1.5 border-b border-line px-4 py-3 md:border-r md:border-b-0">
              <span className="flex flex-wrap items-center gap-2 text-sm">
                <span className={`h-1.5 w-1.5 rounded-full ${engines.claude.paused_until ? "bg-amber-300" : "bg-accent"}`} />
                Claude CLI · claude-opus-5-5
                <span className="ml-auto text-xs text-ink-2">
                  {engines.claude.paused_until ? t("sys.limit_until", { when: when(engines.claude.paused_until) }) : t("sys.available")}
                </span>
              </span>
              <span className="text-xs text-ink-2">
                {t("sys.claude_usage", {
                  runs5: engines.claude.window_5h.runs,
                  tokens: engines.claude.window_5h.tokens.toLocaleString(LOCALE),
                  cost5: engines.claude.window_5h.cost_usd,
                  runs7: engines.claude.window_7d.runs,
                  cost7: engines.claude.window_7d.cost_usd,
                })}
              </span>
              {engines.claude.last_limit?.resets_at && (
                <span className="text-xs text-ink-2">
                  {t("sys.limit_resets", {
                    window: engines.claude.last_limit.window ?? t("sys.window"),
                    state: engines.claude.last_limit.state ?? "",
                    when: when(engines.claude.last_limit.resets_at),
                  })}
                </span>
              )}
            </div>
            <div className="flex min-w-0 flex-col gap-1.5 px-4 py-3">
              <span className="flex flex-wrap items-center gap-2 text-sm">
                <span className={`h-1.5 w-1.5 rounded-full ${engines.codex.can_run ? "bg-accent" : "bg-amber-300"}`} />
                {t("sys.codex")}
                <span className="ml-auto text-xs text-ink-2">{engines.codex.can_run ? t("sys.available") : t("sys.codex_paused")}</span>
              </span>
              <span className="text-xs text-ink-2">
                {t("sys.codex_level", { level: engines.codex.report?.level ? label("budget.level", engines.codex.report.level) : t("sys.no_check") })}
              </span>
            </div>
          </div>
        </Panel>
      )}
      <ObservabilityPanel />
      <SentinelPanel />
      <BudgetPanel />
      <Panel title={t("sys.deploys")} right={t("sys.deploys_right")}>
        {deploys.length === 0 && (
          <p className="px-4 py-3 text-xs leading-relaxed text-ink-2">
            {t("sys.deploys_empty")} <span className="font-mono break-all">docker compose --profile deploy up -d</span> {t("sys.deploys_empty_key")}
          </p>
        )}
        {deploys.map((d) => (
          <div
            key={d.id}
            className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 border-b border-line px-4 py-2 text-[13px] md:grid-cols-[150px_120px_minmax(0,1fr)_110px_120px]"
          >
            <span className="font-mono text-xs">
              {d.old_sha.slice(0, 7)}..{d.new_sha.slice(0, 7)}
            </span>
            <span className={`text-xs ${DEPLOY_COLOR[d.status]} text-right md:text-left`}>
              {label("sys.deploy", d.status)}
              {d.stage ? ` · ${d.stage}` : ""}
            </span>
            <span className="col-span-2 min-w-0 truncate text-ink-2 md:col-span-1" title={d.log}>
              {t("sys.deploy_line", {
                n: d.commits,
                commits: plural(d.commits, "commit", "commity", "commitů"),
                author: d.author || t("sys.unknown"),
              })}
              {d.reverted_sha ? t("sys.deploy_reverted", { sha: d.reverted_sha.slice(0, 7) }) : ""}
            </span>
            <span className="min-w-0">
              {d.task_ref && (
                <Link to={`/tasks?view=agents&task=${d.task_ref}`} className="font-mono text-xs text-accent">
                  {d.task_ref}
                </Link>
              )}
            </span>
            <span className="text-right text-xs text-ink-2">{fmtDateTime(d.created_at)}</span>
          </div>
        ))}
      </Panel>
    </div>
  );
}
