import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { type Engines, agentsApi } from "../agentsApi";
import KnowledgeGraph from "../components/LazyGraph";
import { PageHeader, Panel, SampleBadge } from "../components/ui";
import { GRAPH_LABELS, SUBSYSTEMS } from "../sample";

const HIGHLIGHT = [0, 8, 24, 40, 56];

function Spark({ seed }: { seed: number }) {
  let y = 18;
  let s = seed;
  const pts: string[] = [];
  for (let x = 0; x <= 240; x += 6) {
    s = (s * 9301 + 49297) % 233280;
    y = Math.min(33, Math.max(3, y + (s / 233280 - 0.5) * 8));
    pts.push(`${x},${y.toFixed(1)}`);
  }
  return (
    <svg viewBox="0 0 240 36" className="h-9 w-full" preserveAspectRatio="none" aria-hidden>
      <polyline points={pts.join(" ")} fill="none" stroke="#6cc4dc" strokeWidth="1" vectorEffect="non-scaling-stroke" />
      <line x1="0" y1="35.5" x2="240" y2="35.5" stroke="#232830" />
    </svg>
  );
}

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

export default function System() {
  const [api_, setApi] = useState<{ version: string; phase: string } | null>(null);
  const [deploys, setDeploys] = useState<Deploy[]>([]);
  const [engines, setEngines] = useState<Engines | null>(null);
  useEffect(() => {
    api<{ version: string; phase: string }>("/api/system").then(setApi, () => setApi(null));
    api<Deploy[]>("/api/deploys").then(setDeploys, () => setDeploys([]));
    agentsApi.engines().then(setEngines, () => setEngines(null));
  }, []);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="SYSTEM · PERSONALOS CORE"
        title="Everything PersonalOS knows."
        sub={
          <span className="flex flex-wrap items-center gap-2">
            API {api_ ? `v${api_.version}, phase ${api_.phase}` : "unreachable"}. Graph and subsystem numbers are sample data
            until Phases 2 and 5. <SampleBadge />
          </span>
        }
      />
      <div className="grid grid-cols-1 gap-4 lg:h-[620px] lg:grid-cols-12">
        <Panel
          fig="FIG. 4"
          title="Knowledge graph, full"
          right="96 nodes · sample"
          className="h-[420px] lg:col-span-9 lg:h-auto"
          bodyClassName="measure-grid relative"
        >
          <KnowledgeGraph nodes={96} seed={11} labels={GRAPH_LABELS} highlight={HIGHLIGHT} period={140} distance={2.6} />
          <span className="cap pointer-events-none absolute top-4 left-4 hidden sm:block">drag to orbit · scroll to zoom</span>
        </Panel>
        <div className="flex flex-col gap-4 lg:col-span-3">
          {SUBSYSTEMS.map((s, i) => (
            <Panel key={s.name} bodyClassName="flex flex-col gap-2.5 px-4 py-3.5">
              <span className="flex items-baseline gap-2.5">
                <span className="text-sm font-medium">{s.name}</span>
                <span className="cap ml-auto">{s.proto}</span>
              </span>
              <span className="flex items-baseline gap-2">
                <span className="font-mono text-[26px]">{s.value}</span>
                <span className="cap">{s.unit}</span>
              </span>
              <Spark seed={i + 11} />
              <span className="cap">{s.detail}</span>
            </Panel>
          ))}
        </div>
      </div>
      {engines && (
        <Panel fig="TAB. 18" title="Runtimes and subscriptions" right={`default: ${engines.default} · auto = Codex first, Claude as fallback`}>
          <div className="grid grid-cols-1 md:grid-cols-2">
            <div className="flex flex-col gap-1.5 border-b border-line px-4 py-3 md:border-r md:border-b-0">
              <span className="flex items-center gap-2 text-sm">
                <span className={`h-1.5 w-1.5 rounded-full ${engines.claude.paused_until ? "bg-amber-300" : "bg-accent"}`} />
                Claude CLI · claude-opus-5-5
                <span className="cap ml-auto">{engines.claude.paused_until ? `limit until ${new Date(engines.claude.paused_until).toLocaleString("en-GB")}` : "available"}</span>
              </span>
              <span className="cap">
                5 h: {engines.claude.window_5h.runs} runs · {engines.claude.window_5h.tokens.toLocaleString()} tokens · ${engines.claude.window_5h.cost_usd} · 7 days:{" "}
                {engines.claude.window_7d.runs} runs · ${engines.claude.window_7d.cost_usd}
              </span>
              {engines.claude.last_limit?.resets_at && (
                <span className="cap">
                  {engines.claude.last_limit.window ?? "window"} {engines.claude.last_limit.state ?? ""} · resets{" "}
                  {new Date(engines.claude.last_limit.resets_at).toLocaleString("en-GB")}
                </span>
              )}
            </div>
            <div className="flex flex-col gap-1.5 px-4 py-3">
              <span className="flex items-center gap-2 text-sm">
                <span className={`h-1.5 w-1.5 rounded-full ${engines.codex.can_run ? "bg-accent" : "bg-amber-300"}`} />
                Codex CLI · ChatGPT subscription
                <span className="cap ml-auto">{engines.codex.can_run ? "available" : "paused by Rozpočtář"}</span>
              </span>
              <span className="cap">level: {engines.codex.report?.level ?? "no check yet"}</span>
            </div>
          </div>
        </Panel>
      )}
      <Panel fig="TAB. 17" title="Deploys" right="agents merge to main · the deployer checks, ships or reverts">
        {deploys.length === 0 && (
          <p className="px-4 py-3 text-xs leading-relaxed text-ink-2">
            No deploys recorded yet. On the server the deployer follows main: constitution check, tests, build, health check — and an automatic revert
            commit if anything fails. Start it with <span className="font-mono">docker compose --profile deploy up -d</span> (needs the Deployer key).
          </p>
        )}
        {deploys.map((d) => (
          <div key={d.id} className="grid grid-cols-[150px_90px_minmax(0,1fr)_150px_90px] items-center gap-3 border-b border-line px-4 py-2 text-[13px]">
            <span className="font-mono text-xs">
              {d.old_sha.slice(0, 7)}..{d.new_sha.slice(0, 7)}
            </span>
            <span className={`cap ${DEPLOY_COLOR[d.status]}!`}>
              {d.status}
              {d.stage ? ` · ${d.stage}` : ""}
            </span>
            <span className="truncate text-ink-2" title={d.log}>
              {d.commits} commit{d.commits === 1 ? "" : "s"} by {d.author || "unknown"}
              {d.reverted_sha ? ` · reverted in ${d.reverted_sha.slice(0, 7)}` : ""}
            </span>
            <span>
              {d.task_ref && (
                <Link to={`/tasks?view=agents&task=${d.task_ref}`} className="font-mono text-xs text-accent">
                  {d.task_ref}
                </Link>
              )}
            </span>
            <span className="cap text-right">{new Date(d.created_at).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" })}</span>
          </div>
        ))}
      </Panel>
    </div>
  );
}
