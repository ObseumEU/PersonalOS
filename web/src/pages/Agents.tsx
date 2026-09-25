import { Plus } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { type Agent, agentsApi } from "../agentsApi";
import type { Network } from "../components/agents/AgentNetwork";
import { ActorChip, Pill, StatusDot } from "../components/agents/bits";
import FreezeCard from "../components/agents/FreezeCard";
import { PageHeader, Panel } from "../components/ui";

const AgentNetwork = lazy(() => import("../components/agents/AgentNetwork"));
const input = "h-8 w-full rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

type Rating = { score: number | null; finished: number; quality: number | null; autonomy: number | null; tokens_per_task: number | null };
type Overview = {
  agents: Agent[];
  permissions: Record<string, string>;
  frozen: boolean;
  hr: { kpis?: Record<string, number | null>; ratings: Record<string, Rating>; proposals?: { agent: string; kind: string; reason: string }[]; last_daily?: string | null };
};

export function ago(iso: string | null) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function fmtTokens(n: number) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
}

function AddAgent({ perms, onCreated }: { perms: Record<string, string>; onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", purpose: "", lifetime: "long_lived", instructions: "", budget_class: "normal", a2a_url: "" });
  const [chosen, setChosen] = useState<string[]>(["tasks:read", "tasks:claim", "approvals:request"]);
  const [result, setResult] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setResult(null);
    try {
      const out = await agentsApi.create({ ...form, a2a_url: form.a2a_url || null, runtime: form.a2a_url ? "a2a" : "codex_worker", permissions: chosen });
      if (out.created) {
        setResult(`Created. API key (shown once — give it to the agent): ${out.api_key}`);
        setForm({ ...form, name: "", purpose: "", instructions: "" });
        onCreated();
      } else setResult(`HR stopped it at the ${out.limit}: ${out.decision} — ${out.reason}`);
    } catch (err) {
      setResult(err instanceof Error ? err.message : String(err));
    }
  }

  if (!open)
    return (
      <div className="flex min-h-[220px] flex-col justify-center gap-2.5 rounded-md border border-dashed border-line p-4">
        <span className="text-sm">Add an agent</span>
        <span className="text-xs leading-relaxed text-ink-2">
          Start a Codex CLI worker, or register an outside agent by its A2A card. It gets a bearer key and only the permissions you grant — never more than
          its creator has.
        </span>
        <button type="button" className="btn-accent self-start" onClick={() => setOpen(true)}>
          <Plus size={14} /> New agent
        </button>
      </div>
    );
  return (
    <form onSubmit={submit} className="panel fade-in col-span-full flex flex-col gap-3 p-4">
      <span className="cap text-accent!">NEW AGENT</span>
      <div className="grid gap-3 md:grid-cols-4">
        <label className="flex flex-col gap-1">
          <span className="cap">NAME</span>
          <input required className={input} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1 md:col-span-2">
          <span className="cap">PURPOSE (ONE SENTENCE)</span>
          <input required className={input} value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">LIFETIME</span>
          <select className={input} value={form.lifetime} onChange={(e) => setForm({ ...form, lifetime: e.target.value })}>
            <option value="long_lived">long-lived (waits for work)</option>
            <option value="one_shot">one-shot (retires when done)</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 md:col-span-2">
          <span className="cap">A2A CARD URL (OPTIONAL · OUTSIDE AGENT)</span>
          <input className={input} value={form.a2a_url} placeholder="https://…/.well-known/agent-card.json" onChange={(e) => setForm({ ...form, a2a_url: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">BUDGET CLASS</span>
          <select className={input} value={form.budget_class} onChange={(e) => setForm({ ...form, budget_class: e.target.value })}>
            <option value="normal">normal</option>
            <option value="low">low (slowed first)</option>
            <option value="system">system</option>
          </select>
        </label>
      </div>
      <label className="flex flex-col gap-1">
        <span className="cap">INSTRUCTIONS</span>
        <textarea rows={3} className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent" value={form.instructions} onChange={(e) => setForm({ ...form, instructions: e.target.value })} />
      </label>
      <div className="flex flex-wrap gap-x-4 gap-y-2">
        {Object.entries(perms).map(([p, why]) => (
          <label key={p} title={why} className="flex items-center gap-1.5">
            <input type="checkbox" className="accent-accent" checked={chosen.includes(p)} onChange={(e) => setChosen(e.target.checked ? [...chosen, p] : chosen.filter((x) => x !== p))} />
            <span className="font-mono text-xs">{p}</span>
          </label>
        ))}
      </div>
      <div className="flex gap-2">
        <button className="btn-accent">Create agent</button>
        <button type="button" className="btn" onClick={() => setOpen(false)}>
          Close
        </button>
      </div>
      {result && <p className="cap break-all text-ink!">{result}</p>}
    </form>
  );
}

function Meter({ value, max, warn }: { value: number; max: number; warn?: boolean }) {
  const pct = max ? Math.min(100, (value / max) * 100) : 0;
  return (
    <span className="relative block h-1 w-full rounded-[1px] bg-line">
      <span className={`absolute inset-y-0 left-0 ${warn ? "bg-amber-300" : "bg-accent"}`} style={{ width: `${pct}%` }} />
    </span>
  );
}

const BUILTIN_PURPOSE: Record<string, string> = {
  Assistant: "Answers your questions and does small jobs: reading, summarising, drafting",
  "Knowledge agent": "Research across documents with verified citations (knowlage-agent, A2A)",
  Nexus: "Automations, schedules, retries and outside connectors (Nexus, A2A)",
  "HR agent": "Keeps the team healthy: reviews agents daily, decides at the limits",
};

function Card({ a, rating, maxTokens }: { a: Agent; rating?: Rating; maxTokens: number }) {
  const human = a.kind === "human";
  return (
    <Link
      to={human ? "/board" : `/agents/${a.id}`}
      className={`panel fade-in group flex flex-col gap-3 p-4 transition hover:border-accent ${a.archived ? "opacity-45" : ""}`}
    >
      <span className="flex items-center gap-2">
        <ActorChip a={a} />
        {a.system && <Pill>system</Pill>}
        <span className="ml-auto">
          <StatusDot status={a.status} />
        </span>
      </span>
      <span className="min-h-[2.5em] text-sm leading-snug">{a.purpose ?? BUILTIN_PURPOSE[a.name] ?? (a.is_owner ? "Owner · decides, approves, sets the rules" : "Member")}</span>

      <div className="rounded border border-line bg-bg px-3 py-2">
        <span className="cap">NOW</span>
        <p className="truncate pt-0.5 text-[13px]">
          {a.current ? (
            <>
              <span className="font-mono text-xs text-accent">{a.current.ref}</span> {a.current.title}
            </>
          ) : a.status === "paused" ? (
            "Paused by the owner"
          ) : a.archived ? (
            "Archived"
          ) : (
            <span className="text-ink-3">Waiting for work</span>
          )}
        </p>
      </div>

      <div className="grid grid-cols-4 gap-2 text-center">
        {[
          ["NEXT", a.queued],
          ["WORKING", a.working],
          ["REVIEW", a.review],
          ["DONE 24H", a.done_today],
        ].map(([k, v]) => (
          <span key={k} className="flex flex-col">
            <span className={`font-mono text-lg ${k === "REVIEW" && Number(v) > 0 ? "text-amber-300" : ""}`}>{v}</span>
            <span className="cap text-[9px]!">{k}</span>
          </span>
        ))}
      </div>

      {!human && (
        <div className="flex flex-col gap-1.5">
          <span className="flex justify-between">
            <span className="cap">TOKENS 24H</span>
            <span className="cap text-ink-2!">
              {fmtTokens(a.tokens_24h)}
              {a.daily_cap ? ` / ${fmtTokens(a.daily_cap)}` : ""} · {a.budget_class ?? "normal"}
            </span>
          </span>
          <Meter value={a.tokens_24h} max={a.daily_cap ?? Math.max(maxTokens, 1)} warn={!!a.daily_cap && a.tokens_24h > a.daily_cap * 0.8} />
        </div>
      )}

      <div className="grid grid-cols-[76px_minmax(0,1fr)] gap-x-2 gap-y-1 text-xs">
        <span className="cap">RUNTIME</span>
        <span className="truncate text-ink-2">{a.runtime}</span>
        {!human && (
          <>
            <span className="cap">LIFETIME</span>
            <span className="text-ink-2">{a.lifetime ?? "built-in"}</span>
            <span className="cap">CREATED BY</span>
            <span className="text-ink-2">{a.created_by_name ?? "platform"}</span>
          </>
        )}
        <span className="cap">HEARTBEAT</span>
        <span className="text-ink-2">{human ? "—" : ago(a.last_seen_at)}</span>
        {rating && (
          <>
            <span className="cap">HR SCORE</span>
            <span className="text-ink-2">
              {rating.score == null ? "not enough work yet" : `${Math.round(rating.score * 100)} / 100 · ${rating.finished} finished`}
            </span>
          </>
        )}
      </div>

      <div className="mt-auto flex flex-wrap gap-1">
        {a.permissions.map((p) => (
          <Pill key={p}>{p}</Pill>
        ))}
        {a.approvals_waiting > 0 && <Pill warn>{`${a.approvals_waiting} approval waiting`}</Pill>}
      </div>
    </Link>
  );
}

export default function Agents() {
  const [data, setData] = useState<Overview | null>(null);
  const [net, setNet] = useState<Network | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const navigate = useNavigate();
  const load = useCallback(() => {
    agentsApi.list().then((d) => setData(d as Overview));
    agentsApi.network("live").then(setNet);
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, 15000); // live status
    return () => clearInterval(t);
  }, [load]);

  const list = data?.agents.filter((a) => showArchived || !a.archived) ?? [];
  const active = data?.agents.filter((a) => a.kind !== "human" && !a.archived) ?? [];
  const maxTokens = Math.max(0, ...active.map((a) => a.tokens_24h));
  const k = data?.hr.kpis ?? {};
  const onSelect = useCallback((id: number) => navigate(`/agents/${id}`), [navigate]);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="PLATFORM · MEMBERS · PEOPLE AND AGENTS"
        title="Agents"
        sub="Everyone works on the same tasks — you in the web app, agents through MCP and A2A — each with their own permissions."
      />

      <div className="grid gap-4 lg:grid-cols-12">
        <Panel fig="FIG. 5" title="Agent network" right={<Link to="/network" className="hover:text-accent">live · full view →</Link>} className="h-[380px] lg:col-span-8" bodyClassName="measure-grid relative">
          <Suspense fallback={<p className="cap breathe absolute inset-0 grid place-items-center">loading network…</p>}>
            {net && <AgentNetwork data={net} onSelect={onSelect} compact />}
          </Suspense>
          <div className="pointer-events-none absolute bottom-3 left-4 flex flex-wrap gap-4">
            <span className="cap flex items-center gap-1.5"><span className="h-[7px] w-[7px] rounded-full bg-ink" />person</span>
            <span className="cap flex items-center gap-1.5"><span className="h-[7px] w-[7px] rounded-full bg-accent" />agent · size = workload</span>
            <span className="cap flex items-center gap-1.5"><span className="h-[7px] w-[7px] rounded-full bg-amber-300" />waiting for your review</span>
          </div>
        </Panel>

        <div className="flex flex-col gap-4 lg:col-span-4">
          <FreezeCard onChange={load} />
          <Panel fig="TAB. 7" title="Team this week" right={data?.hr.last_daily ? `HR review ${ago(data.hr.last_daily)}` : "HR"} bodyClassName="grid grid-cols-2">
            {[
              ["ACTIVE AGENTS", String(active.length)],
              ["DONE BY AGENTS", String(k.tasks_completed ?? 0)],
              ["UNASSISTED", k.unassisted_rate == null ? "—" : `${Math.round(Number(k.unassisted_rate) * 100)} %`],
              ["RETURNED", k.returned_rate == null ? "—" : `${Math.round(Number(k.returned_rate) * 100)} %`],
              ["YOUR INTERVENTIONS", String(k.owner_interventions ?? 0)],
              ["TOKENS", fmtTokens(Number(k.tokens_used ?? 0))],
            ].map(([label, value]) => (
              <div key={label} className="flex flex-col gap-1 border-r border-b border-line px-4 py-3 [&:nth-child(2n)]:border-r-0">
                <span className="cap">{label}</span>
                <span className="font-mono text-xl">{value}</span>
              </div>
            ))}
          </Panel>
          {(data?.hr.proposals?.length ?? 0) > 0 && (
            <Panel fig="HR" title="Proposals" right="from the HR agent">
              {data!.hr.proposals!.slice(0, 4).map((p, i) => (
                <p key={i} className="border-b border-line px-4 py-2 text-xs text-ink-2 last:border-0">
                  <span className="font-mono text-accent">{p.kind}</span> {p.agent}: {p.reason}
                </p>
              ))}
            </Panel>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <span className="cap">TAB. 8 · MEMBERS</span>
        <Link to="/board" className="btn">
          Work board →
        </Link>
        <Link to="/approvals" className="btn">
          Approvals →
        </Link>
        <label className="cap ml-auto flex items-center gap-1.5">
          <input type="checkbox" className="accent-accent" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} /> show archived
        </label>
      </div>
      <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4">
        {list.map((a) => (
          <Card key={a.id} a={a} rating={data?.hr.ratings[String(a.id)]} maxTokens={maxTokens} />
        ))}
        {data && <AddAgent perms={data.permissions} onCreated={load} />}
      </div>
    </div>
  );
}
