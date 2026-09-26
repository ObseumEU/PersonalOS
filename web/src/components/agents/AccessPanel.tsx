import { Check, Plus, X } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { type AccessEvent, type AccessRequest, accessApi, type AgentAccess, type BudgetLine, type CompanyAccess, fmtMetric } from "../../accessApi";
import { ago } from "../../pages/Agents";
import { until } from "../Schedules";
import Markdown from "../Markdown";
import { Panel } from "../ui";
import { Pill } from "./bits";

const field = "h-7 rounded border border-line bg-bg px-2 font-mono text-xs outline-none focus:border-accent";

function useAct(reload: () => void) {
  const [error, setError] = useState<string | null>(null);
  const act = async (p: Promise<unknown>) => {
    try {
      await p;
      setError(null);
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return { error, act };
}

/** One line per limit: what is used against it, until when a temporary one holds. */
function Budgets({ budgets }: { budgets: Record<string, BudgetLine> }) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-3">
      {Object.entries(budgets).map(([m, b]) => {
        const pct = b.limit && b.used != null ? Math.min(100, Math.round((100 * b.used) / b.limit)) : null;
        return (
          <div key={m} title={b.reason ? `${b.reason}${b.set_by ? ` — ${b.set_by}` : ""}` : undefined} className="flex flex-col gap-1 border-r border-b border-line px-4 py-2">
            <span className="cap">{b.label}</span>
            <span className="font-mono text-sm">
              {b.used != null ? `${fmtMetric(m, b.used)} / ` : ""}
              {fmtMetric(m, b.limit)}
            </span>
            {pct != null && (
              <span className="h-1 rounded bg-line">
                <span className={`block h-1 rounded ${pct >= 100 ? "bg-red-400" : pct >= 80 ? "bg-amber-300" : "bg-accent"}`} style={{ width: `${pct}%` }} />
              </span>
            )}
            {b.expires_at && <span className="cap text-amber-300!">temporary · ends {until(b.expires_at)}</span>}
          </div>
        );
      })}
    </div>
  );
}

function requestLabel(r: AccessRequest) {
  if (r.what === "capability") return r.capability ?? "";
  if (r.what === "budget") return `${r.metric} ${r.amount ?? "(raise)"}`;
  return "spend spike review";
}

/** Pending requests; the owner can decide any of them (the Access manager usually does). */
function Requests({ items, onDecide }: { items: AccessRequest[]; onDecide: (r: AccessRequest, d: "grant" | "deny") => void }) {
  if (items.length === 0) return <p className="cap px-4 py-3">No open requests.</p>;
  return (
    <>
      {items.map((r) => (
        <div key={r.id} className="flex flex-col gap-1 border-b border-line px-4 py-2">
          <span className="flex flex-wrap items-center gap-2">
            <span className="cap">#{r.id}</span>
            <span className="text-[13px]">{r.agent_name}</span>
            <span className="font-mono text-xs text-accent">{requestLabel(r)}</span>
            {r.hours ? <Pill>{`${r.hours} h`}</Pill> : null}
            {r.trigger !== "request" && <Pill warn>{r.trigger.replace("_", " ")}</Pill>}
            {r.needs_owner && <Pill warn>owner only</Pill>}
            {r.status === "escalated" && <Pill warn>escalated</Pill>}
            {r.task_ref && <span className="cap">{r.task_ref}</span>}
            <span className="cap ml-auto">{ago(r.created_at)}</span>
            <button className="btn h-6! px-2!" title="Grant as asked" onClick={() => onDecide(r, "grant")}>
              <Check size={12} />
            </button>
            <button className="btn h-6! px-2!" title="Deny" onClick={() => onDecide(r, "deny")}>
              <X size={12} />
            </button>
          </span>
          <Markdown text={r.why} compact className="text-xs text-ink-2" />
        </div>
      ))}
    </>
  );
}

function describe(e: AccessEvent): string {
  const d = e.detail as Record<string, string | number | null | undefined>;
  const what = d.capability ?? (d.metric ? `${d.metric}${d.amount !== undefined ? ` → ${d.amount ?? "no limit"}` : ""}` : d.request ? `#${d.request}` : "");
  const why = d.reason ?? d.note;
  return `**${e.action.replace(/^access_/, "")}** \`${what}\`${d.hours ? ` for ${d.hours} h` : ""}${why ? ` — ${why}` : ""}`;
}

function History({ items }: { items: AccessEvent[] }) {
  if (items.length === 0) return <p className="cap px-4 py-3">No decisions yet.</p>;
  return (
    <>
      {items.map((e) => (
        <div key={e.id} className="grid grid-cols-[90px_110px_minmax(0,1fr)] gap-2 border-b border-line px-4 py-1.5 text-xs">
          <span className="cap">{ago(e.at)}</span>
          <span className="truncate text-ink-2">{e.actor_name ?? "platform"}</span>
          <Markdown text={describe(e)} compact />
        </div>
      ))}
    </>
  );
}

/** Grants, budgets, open requests and the decision history of one agent (its page). */
export function AccessPanel({ agentId }: { agentId: number }) {
  const [v, setV] = useState<AgentAccess | null>(null);
  const load = useCallback(() => {
    accessApi.agent(agentId).then(setV, () => setV(null));
  }, [agentId]);
  useEffect(load, [load]);
  const { error, act } = useAct(load);
  const [cap, setCap] = useState("");
  const [hours, setHours] = useState("");
  const [reason, setReason] = useState("");
  const [metric, setMetric] = useState("usd_day");
  const [amount, setAmount] = useState("");

  if (!v) return null;
  const ask = (what: string) => window.prompt(`Reason (${what}) — it goes to the audit log and #team`)?.trim();
  const grant = (e: FormEvent) => {
    e.preventDefault();
    if (!cap.trim() || !reason.trim()) return;
    act(accessApi.grant(agentId, cap.trim(), reason.trim(), hours ? Number(hours) : null)).then(() => {
      setCap("");
      setReason("");
      setHours("");
    });
  };
  const budget = (e: FormEvent) => {
    e.preventDefault();
    const why = ask(`${metric} → ${amount || "no limit"}`);
    if (why) act(accessApi.budget(agentId, metric, amount ? Number(amount) : null, why, hours ? Number(hours) : null));
  };
  const decide = (r: AccessRequest, d: "grant" | "deny") => {
    const why = ask(`${d} #${r.id}`);
    if (why) act(accessApi.decide(r.id, d, why));
  };

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <Panel fig="ACCESS" title="Grants" right={v.is_manager ? "its own grants: owner only" : "Access manager or owner · expiring grants revert on their own"} className="lg:col-span-5" bodyClassName="max-h-[380px] overflow-y-auto">
        {!v.managed && <p className="cap px-4 py-2">Not managed yet: permissions come from the checkboxes below.</p>}
        {v.grants.map((g) => (
          <div key={g.id} className="flex items-center gap-2 border-b border-line px-4 py-1.5 text-xs" title={`${g.reason} — ${g.granted_by_name ?? g.source}`}>
            <span className="font-mono text-accent">{g.capability}</span>
            {g.kind === "outbound" && <Pill warn>each send approved</Pill>}
            <span className="cap truncate">{g.expires_at ? `ends ${until(g.expires_at)}` : "permanent"} · {g.granted_by_name ?? g.source}</span>
            <button
              className="ml-auto text-ink-3 hover:text-red-400"
              title="Revoke"
              onClick={() => {
                const why = ask(`revoke ${g.capability}`);
                if (why) act(accessApi.revoke(g.id, why));
              }}
            >
              <X size={13} />
            </button>
          </div>
        ))}
        <form onSubmit={grant} className="flex flex-wrap items-end gap-2 p-3">
          <input aria-label="Capability" placeholder="tool:create_task · tasks:write · outbound:email.send" value={cap} onChange={(e) => setCap(e.target.value)} className={`${field} w-64`} />
          <input aria-label="Hours" placeholder="hours (empty = permanent)" value={hours} onChange={(e) => setHours(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-40`} />
          <input aria-label="Reason" placeholder="reason" value={reason} onChange={(e) => setReason(e.target.value)} className={`${field} min-w-40 flex-1`} />
          <button className="btn">
            <Plus size={13} /> Grant
          </button>
        </form>
      </Panel>
      <Panel fig="BUDGET" title="Budgets" right="used / limit · 24 h and this month" className="lg:col-span-7">
        <Budgets budgets={v.budgets} />
        <form onSubmit={budget} className="flex flex-wrap items-end gap-2 p-3">
          <select aria-label="Metric" value={metric} onChange={(e) => setMetric(e.target.value)} className={field}>
            {Object.entries(v.budgets).map(([m, b]) => (
              <option key={m} value={m}>
                {b.label}
              </option>
            ))}
          </select>
          <input aria-label="Amount" placeholder="amount (empty = no limit)" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-44`} />
          <button className="btn">Set limit</button>
          <span className="cap">uses the hours field for a temporary raise</span>
        </form>
      </Panel>
      <Panel fig="REQUESTS" title="Open requests" right={`${v.requests.length} open`} className="lg:col-span-5" bodyClassName="max-h-[300px] overflow-y-auto">
        <Requests items={v.requests} onDecide={decide} />
      </Panel>
      <Panel fig="AUDIT" title="Access history" right="every decision with its reason" className="lg:col-span-7" bodyClassName="max-h-[300px] overflow-y-auto">
        <History items={v.history} />
      </Panel>
      {error && <p className="cap text-red-400! lg:col-span-12">{error}</p>}
    </div>
  );
}

/** The owner's view on the Access manager's page: the company cap, every open request, the settings. */
export function CompanyAccessPanel() {
  const [c, setC] = useState<CompanyAccess | null>(null);
  const load = useCallback(() => {
    accessApi.company().then(setC, () => setC(null));
  }, []);
  useEffect(load, [load]);
  const { error, act } = useAct(load);
  const [metric, setMetric] = useState("usd_day");
  const [amount, setAmount] = useState("");
  if (!c) return null;
  const decide = (r: AccessRequest, d: "grant" | "deny") => {
    const why = window.prompt(`Reason (${d} #${r.id})`)?.trim();
    if (why) act(accessApi.decide(r.id, d, why));
  };
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <Panel fig="CAP" title="Company-wide cap" right={`owner only${c.frozen ? " · KILL SWITCH ON" : ""}${c.litellm ? " · LiteLLM on" : ""}`} className="lg:col-span-7">
        <Budgets budgets={Object.fromEntries(Object.entries(c.budgets).filter(([m]) => m !== "usd_run"))} />
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const why = window.prompt(`Reason (company cap ${metric} → ${amount || "no cap"})`)?.trim();
            if (why) act(accessApi.budget(null, metric, amount ? Number(amount) : null, why, null));
          }}
          className="flex flex-wrap items-end gap-2 p-3"
        >
          <select aria-label="Cap metric" value={metric} onChange={(e) => setMetric(e.target.value)} className={field}>
            {Object.entries(c.metrics)
              .filter(([m]) => m !== "usd_run")
              .map(([m, label]) => (
                <option key={m} value={m}>
                  {label}
                </option>
              ))}
          </select>
          <input aria-label="Cap amount" placeholder="amount (empty = no cap)" value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-44`} />
          <button className="btn">Set cap</button>
        </form>
        <div className="flex flex-wrap items-center gap-3 border-t border-line px-4 py-2 text-xs">
          <span className="cap">SPIKE WATCH</span>
          {Object.entries(c.settings).map(([k, val]) => (
            <label key={k} className="flex items-center gap-1">
              <span className="cap">{k.replace(/_/g, " ")}</span>
              <input
                key={`${k}-${val}`}
                aria-label={k}
                defaultValue={val}
                onBlur={(e) => Number(e.target.value) !== val && act(accessApi.settings({ [k]: Number(e.target.value) }))}
                className={`${field} w-20`}
              />
            </label>
          ))}
        </div>
      </Panel>
      <Panel fig="QUEUE" title="All open requests" right="the Access manager decides; you can too" className="lg:col-span-5" bodyClassName="max-h-[340px] overflow-y-auto">
        <Requests items={c.requests} onDecide={decide} />
      </Panel>
      <Panel fig="AUDIT" title="Recent access decisions (all agents)" className="lg:col-span-12" bodyClassName="max-h-[300px] overflow-y-auto">
        <History items={c.history} />
      </Panel>
      {error && <p className="cap text-red-400! lg:col-span-12">{error}</p>}
    </div>
  );
}
