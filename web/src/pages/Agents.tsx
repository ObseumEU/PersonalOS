import { Plus } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { type Agent, agentsApi } from "../agentsApi";
import { ActorChip, EngineBadge, Pill, StatusDot } from "../components/agents/bits";
import FreezeCard from "../components/agents/FreezeCard";
import { WorkingOnText, workingOn } from "../components/agents/WorkingOn";
import HiringPanel, { InvitePanel } from "../components/Hiring";
import { Panel } from "../components/ui";
import { ago as agoCs, label, t } from "../i18n";
import { StructureList } from "./Org";
import { useLiveReload } from "../liveStream";

const input = "h-8 w-full rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

type Rating = { score: number | null; finished: number; quality: number | null; autonomy: number | null; tokens_per_task: number | null; cost_per_task?: number | null };
type Overview = {
  agents: Agent[];
  permissions: Record<string, string>;
  frozen: boolean;
  hr: { kpis?: Record<string, number | null>; ratings: Record<string, Rating>; proposals?: { agent: string; kind: string; reason: string }[]; last_daily?: string | null };
};

/** "před 5 min" (kept here for the pages that import it from Agents). */
export const ago = agoCs;

export function fmtTokens(n: number) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
}

function AddAgent({ perms, onCreated }: { perms: Record<string, string>; onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", purpose: "", lifetime: "long_lived", instructions: "", budget_class: "normal", a2a_url: "" });
  const [chosen, setChosen] = useState<string[]>(["tasks:read", "tasks:claim", "approvals:request"]);
  const [advanced, setAdvanced] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setResult(null);
    try {
      const out = await agentsApi.create({ ...form, a2a_url: form.a2a_url || null, runtime: form.a2a_url ? "a2a" : "codex_worker", permissions: chosen });
      if (out.created) {
        setResult(t("agents.add.created", { key: out.api_key }));
        setForm({ ...form, name: "", purpose: "", instructions: "" });
        onCreated();
      } else setResult(t("agents.add.stopped", { limit: out.limit, decision: out.decision, reason: out.reason }));
    } catch (err) {
      setResult(err instanceof Error ? err.message : String(err));
    }
  }

  if (!open)
    return (
      <div className="flex min-h-[220px] flex-col justify-center gap-2.5 rounded-md border border-dashed border-line p-4">
        <span className="text-sm">{t("agents.add.title")}</span>
        <span className="text-xs leading-relaxed text-ink-2">{t("agents.add.blurb")}</span>
        <button type="button" className="btn-accent self-start" onClick={() => setOpen(true)}>
          <Plus size={14} /> {t("agents.add.new")}
        </button>
      </div>
    );
  return (
    <form onSubmit={submit} className="panel fade-in col-span-full flex flex-col gap-3 p-4">
      <span className="text-sm font-medium text-accent">{t("agents.add.new")}</span>
      <div className="grid gap-3 md:grid-cols-4">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-2">{t("agents.add.name")}</span>
          <input required className={input} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1 md:col-span-2">
          <span className="text-xs text-ink-2">{t("agents.add.purpose")}</span>
          <input required className={input} value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-2">{t("agents.add.lifetime")}</span>
          <select className={input} value={form.lifetime} onChange={(e) => setForm({ ...form, lifetime: e.target.value })}>
            <option value="long_lived">{t("agents.lifetime.long_lived")}</option>
            <option value="one_shot">{t("agents.lifetime.one_shot")}</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 md:col-span-2">
          <span className="text-xs text-ink-2">{t("agents.add.a2a")}</span>
          <input className={input} value={form.a2a_url} placeholder="https://…/.well-known/agent-card.json" onChange={(e) => setForm({ ...form, a2a_url: e.target.value })} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-2">{t("agents.add.budget")}</span>
          <select className={input} value={form.budget_class} onChange={(e) => setForm({ ...form, budget_class: e.target.value })}>
            <option value="normal">{t("agents.budget.normal")}</option>
            <option value="low">{t("agents.budget.low")}</option>
            <option value="system">{t("agents.budget.system")}</option>
          </select>
        </label>
      </div>
      <label className="flex flex-col gap-1">
        <span className="text-xs text-ink-2">{t("agents.add.instructions")}</span>
        <textarea rows={3} className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent" value={form.instructions} onChange={(e) => setForm({ ...form, instructions: e.target.value })} />
      </label>
      <div className="flex flex-col gap-2">
        <span className="text-xs text-ink-2">{t("agents.add.permissions")}</span>
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {Object.entries(perms).map(([p, why]) => (
            <label key={p} title={advanced ? why : p} className="flex items-center gap-1.5 text-[13px]">
              <input type="checkbox" className="accent-accent" checked={chosen.includes(p)} onChange={(e) => setChosen(e.target.checked ? [...chosen, p] : chosen.filter((x) => x !== p))} />
              {label("perm", p)}
              {advanced && <span className="font-mono text-xs text-ink-2">{p}</span>}
            </label>
          ))}
        </div>
        <button type="button" className="self-start text-xs text-ink-2 underline" onClick={() => setAdvanced((a) => !a)}>
          {t("act.advanced")}
        </button>
      </div>
      <div className="flex gap-2">
        <button className="btn-accent">{t("agents.add.create")}</button>
        <button type="button" className="btn" onClick={() => setOpen(false)}>
          {t("act.close")}
        </button>
      </div>
      {result && <p className="text-xs break-all text-ink">{result}</p>}
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

function Card({ a, rating, maxTokens }: { a: Agent; rating?: Rating; maxTokens: number }) {
  const human = a.kind === "human";
  const w = workingOn(a);
  return (
    <div className={`panel fade-in group flex flex-col gap-3 p-4 transition hover:border-accent ${a.archived ? "border-dashed" : ""}`}>
      <span className="flex flex-wrap items-center gap-2">
        <Link to={`/team/${a.id}`} className="hover:underline">
          <ActorChip a={a} />
        </Link>
        {a.system && <Pill>{t("agents.system")}</Pill>}
        {a.archived && <Pill>{t("status.archived")}</Pill>}
        {a.kind !== "human" && <EngineBadge view={a.engine_view} engine={a.engine_effective} model={a.engine_effective === "auto" || a.engine_effective === "claude" ? a.model ?? "claude-opus-5-5" : null} />}
        <span className="ml-auto">
          <StatusDot status={a.status} />
        </span>
      </span>
      <Link to={`/team/${a.id}`} className="min-h-[2.5em] text-sm leading-snug hover:text-accent">
        {a.purpose ?? (a.is_owner ? t("agents.owner_purpose") : t("agents.member"))}
      </Link>

      <div className="rounded border border-line bg-bg px-3 py-2">
        <span className="text-xs text-ink-2">{t("agents.now")}</span>
        <p className="truncate pt-0.5 text-[13px]">
          {w ? (
            <WorkingOnText w={w} withTitle />
          ) : a.status === "paused" ? (
            t("agents.paused_by_owner")
          ) : a.archived ? (
            t("status.archived")
          ) : (
            <span className="text-ink-2">{t("agents.waiting_for_work")}</span>
          )}
        </p>
      </div>

      <div className="grid grid-cols-4 gap-2 text-center">
        {(
          [
            ["agents.stat.next", a.queued],
            ["agents.stat.working", a.working],
            ["agents.stat.review", a.review],
            ["agents.stat.done", a.done_today],
          ] as const
        ).map(([k, v]) => (
          <span key={k} className="flex flex-col">
            <span className={`font-mono text-lg ${k === "agents.stat.review" && Number(v) > 0 ? "text-amber-300" : ""}`}>{v}</span>
            <span className="text-xs text-ink-2">{t(k)}</span>
          </span>
        ))}
      </div>

      {!human && (
        <div className="flex flex-col gap-1.5">
          <span className="flex justify-between text-xs text-ink-2">
            <span>{t("agents.tokens_24h")}</span>
            <span>
              {fmtTokens(a.tokens_24h)}
              {a.daily_cap ? ` / ${fmtTokens(a.daily_cap)}` : ""}
            </span>
          </span>
          <Meter value={a.tokens_24h} max={a.daily_cap ?? Math.max(maxTokens, 1)} warn={!!a.daily_cap && a.tokens_24h > a.daily_cap * 0.8} />
        </div>
      )}

      <div className="grid grid-cols-[96px_minmax(0,1fr)] gap-x-2 gap-y-1 text-xs">
        <span className="text-ink-2">{t("agents.runtime")}</span>
        <span className="truncate">{label("runtime", a.runtime)}</span>
        <span className="text-ink-2">{t("agents.heartbeat")}</span>
        <span>{human ? "—" : ago(a.last_seen_at)}</span>
        {rating && (
          <>
            <span className="text-ink-2">{t("agents.hr_score")}</span>
            <span>
              {rating.score == null ? t("agents.hr_not_enough") : t("agents.hr_value", { score: Math.round(rating.score * 100), n: rating.finished })}
            </span>
          </>
        )}
      </div>
      {a.approvals_waiting > 0 && (
        <span className="mt-auto">
          <Pill warn>{t("agents.approvals_waiting", { n: a.approvals_waiting })}</Pill>
        </span>
      )}
    </div>
  );
}

export default function Agents() {
  const [data, setData] = useState<Overview | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const load = useCallback(() => {
    agentsApi.list().then((d) => setData(d as Overview));
  }, []);
  useEffect(() => {
    load();
  }, [load]);
  useLiveReload(["actor", "run", "task"], load); // live status

  const list = data?.agents.filter((a) => showArchived || !a.archived) ?? [];
  const archivedCount = data?.agents.filter((a) => a.archived).length ?? 0;
  const active = data?.agents.filter((a) => a.kind !== "human" && !a.archived) ?? [];
  const maxTokens = Math.max(0, ...active.map((a) => a.tokens_24h));
  const k = data?.hr.kpis ?? {};

  return (
    <div className="flex flex-col gap-5">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <StructureList className="max-h-[420px] min-w-0 overflow-y-auto lg:col-span-8" />

        <div className="flex min-w-0 flex-col gap-4 lg:col-span-4">
          <FreezeCard onChange={load} />
          <Panel title={t("agents.week")} right={data?.hr.last_daily ? t("agents.hr_review", { ago: ago(data.hr.last_daily) }) : undefined} bodyClassName="grid grid-cols-2">
            {[
              ["agents.kpi.active", String(active.length)],
              ["agents.kpi.done", String(k.tasks_completed ?? 0)],
              ["agents.kpi.unassisted", k.unassisted_rate == null ? "—" : `${Math.round(Number(k.unassisted_rate) * 100)} %`],
              ["agents.kpi.returned", k.returned_rate == null ? "—" : `${Math.round(Number(k.returned_rate) * 100)} %`],
              ["agents.kpi.interventions", String(k.owner_interventions ?? 0)],
              ["agents.kpi.tokens", fmtTokens(Number(k.tokens_used ?? 0))],
              ["agents.kpi.cost", `$${Number(k.cost_usd ?? 0).toFixed(2)}`],
            ].map(([key, value]) => (
              <div key={key} className="flex flex-col gap-1 border-r border-b border-line px-4 py-3 [&:nth-child(2n)]:border-r-0">
                <span className="text-xs text-ink-2">{t(key)}</span>
                <span className="font-mono text-xl">{value}</span>
              </div>
            ))}
          </Panel>
          {(data?.hr.proposals?.length ?? 0) > 0 && (
            <Panel title={t("agents.proposals")} right={t("agents.proposals_from")}>
              {data!.hr.proposals!.slice(0, 4).map((p, i) => (
                <p key={i} className="border-b border-line px-4 py-2 text-xs text-ink-2 last:border-0">
                  <span className="font-medium text-accent">{p.kind}</span> {p.agent}: {p.reason}
                </p>
              ))}
            </Panel>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-medium">{t("agents.members")}</h2>
        <label className="ml-auto flex items-center gap-1.5 text-xs text-ink-2">
          <input type="checkbox" className="accent-accent" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
          {t("act.show_archived")}
          {archivedCount > 0 ? ` (${archivedCount})` : ""}
        </label>
      </div>
      <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-4">
        {list.map((a) => (
          <Card key={a.id} a={a} rating={data?.hr.ratings[String(a.id)]} maxTokens={maxTokens} />
        ))}
        {data && <AddAgent perms={data.permissions} onCreated={load} />}
      </div>
      {data && <HiringPanel members={data.agents.filter((a) => !a.archived).map((a) => ({ id: a.id, name: a.name, kind: a.kind }))} onHired={load} />}
      <InvitePanel />
    </div>
  );
}
