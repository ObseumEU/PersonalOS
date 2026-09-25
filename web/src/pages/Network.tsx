import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { agentsApi } from "../agentsApi";
import type { EdgeType, Network as Net } from "../components/agents/AgentNetwork";
import { PageHeader, Panel } from "../components/ui";
import { ago, fmtTokens } from "./Agents";

const AgentNetwork = lazy(() => import("../components/agents/AgentNetwork"));

const LEGEND: [EdgeType, string, string][] = [
  ["assign", "#e6e8eb", "hand-off of a task"],
  ["message", "#6cc4dc", "message"],
  ["approval", "#d9a55b", "approval request"],
  ["mcp", "#3e7c8d", "calls to PersonalOS (MCP)"],
  ["run", "#6cc4dc", "codex run"],
];
const WINDOWS = [
  ["live", "Live · 1 h"],
  ["24h", "24 h"],
  ["7d", "7 days"],
] as const;

const fetchNetwork = agentsApi.network;

export default function NetworkPage() {
  const [params, setParams] = useSearchParams();
  const window_ = params.get("window") ?? "live";
  const [data, setData] = useState<Net | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    let alive = true;
    const load = () => fetchNetwork(window_).then((d) => alive && setData(d));
    load();
    const t = setInterval(load, window_ === "live" ? 10000 : 60000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [window_]);
  const onSelect = useCallback((id: number) => navigate(`/agents/${id}`), [navigate]);

  const names = new Map(data?.nodes.map((n) => [n.id, n.is_owner ? "You" : n.name]));
  const ranked = [...(data?.nodes ?? [])]
    .filter((n) => n.kind !== "hub" && n.status !== "archived")
    .sort((a, b) => b.open + 2 * b.working + b.review - (a.open + 2 * a.working + a.review));
  const totals = LEGEND.map(([t]) => [t, data?.edges.filter((e) => e.type === t).reduce((s, e) => s + e.count, 0) ?? 0] as const);

  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader
        kicker="PLATFORM · AGENT NETWORK"
        title="How the team works together"
        sub="Every member — you, other people and agents — sized by workload. Lines show hand-offs, messages, approvals and calls to PersonalOS; dots travel along them for the latest events."
      />
      <div className="flex flex-wrap items-center gap-2">
        {WINDOWS.map(([id, label]) => (
          <button key={id} type="button" onClick={() => setParams({ window: id })} className={id === window_ ? "btn-accent" : "btn"}>
            {label}
          </button>
        ))}
        {data?.frozen && <span className="cap ml-2 rounded-sm border border-amber-400/60 px-2 py-1 text-amber-300!">KILL SWITCH ON · agents frozen</span>}
        <Link to="/agents" className="btn ml-auto">
          Agents →
        </Link>
        <Link to="/board" className="btn">
          Work board →
        </Link>
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel fig="FIG. 6" title="Agent network" right={`window ${window_} · drag to orbit · click a member`} className="h-[520px] lg:col-span-9 lg:h-auto" bodyClassName="measure-grid relative">
          <Suspense fallback={<p className="cap breathe absolute inset-0 grid place-items-center">loading network…</p>}>
            {data && <AgentNetwork data={data} onSelect={onSelect} />}
          </Suspense>
          <div className="pointer-events-none absolute bottom-3 left-4 flex flex-wrap gap-4">
            {LEGEND.map(([t, color, label]) => (
              <span key={t} className="cap flex items-center gap-1.5">
                <span className="h-px w-4" style={{ background: color }} />
                {label}
              </span>
            ))}
            <span className="cap flex items-center gap-1.5"><span className="h-[7px] w-[7px] rounded-full bg-dim" />paused / frozen / archived</span>
          </div>
        </Panel>
        <div className="flex min-h-0 flex-col gap-4 lg:col-span-3 lg:overflow-y-auto">
          <Panel fig="TAB. 9" title="Workload" right="open · working · review">
            {ranked.map((n) => {
              const load = n.open + 2 * n.working + n.review;
              const max = Math.max(1, ...ranked.map((r) => r.open + 2 * r.working + r.review));
              return (
                <Link key={n.id} to={n.kind === "human" ? "/board" : `/agents/${n.id}`} className="flex flex-col gap-1 border-b border-line px-4 py-2 last:border-0 hover:bg-raised">
                  <span className="flex items-baseline gap-2 text-[13px]">
                    {n.is_owner ? "You" : n.name}
                    <span className="cap ml-auto">
                      {n.open}·{n.working}·{n.review}
                      {n.tokens ? ` · ${fmtTokens(n.tokens)} tok` : ""}
                    </span>
                  </span>
                  <span className="relative block h-1 bg-line">
                    <span className={`absolute inset-y-0 left-0 ${n.review ? "bg-amber-300" : n.kind === "human" ? "bg-ink" : "bg-accent"}`} style={{ width: `${(load / max) * 100}%` }} />
                  </span>
                </Link>
              );
            })}
          </Panel>
          <Panel fig="TAB. 10" title="Interactions" right={window_}>
            <div className="grid grid-cols-2">
              {totals.map(([t, n]) => (
                <div key={t} className="flex flex-col border-r border-b border-line px-4 py-2 [&:nth-child(2n)]:border-r-0">
                  <span className="cap">{t.toUpperCase()}</span>
                  <span className="font-mono text-lg">{n}</span>
                </div>
              ))}
            </div>
          </Panel>
          <Panel fig="LOG" title="Latest" className="min-h-0 flex-1" bodyClassName="overflow-y-auto">
            {data?.events.slice(0, 25).map((e, i) => (
              <div key={i} className="grid grid-cols-[64px_minmax(0,1fr)] gap-2 border-b border-line px-4 py-1.5 font-mono text-[11px]">
                <span className="text-ink-3">{ago(e.at)}</span>
                <span className="truncate">
                  <span className="text-ink-2">{names.get(e.from)}</span> <span className="text-accent">{e.type}</span> → <span className="text-ink-2">{names.get(e.to)}</span>
                </span>
              </div>
            ))}
            {data?.events.length === 0 && <p className="cap px-4 py-3">Nothing in this window yet.</p>}
          </Panel>
        </div>
      </div>
    </div>
  );
}
