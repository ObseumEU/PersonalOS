import { Check, Plus, X } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { type AccessEvent, type AccessRequest, accessApi, type AgentAccess, type BudgetLine, type CompanyAccess, fmtMetric } from "../../accessApi";
import { ago, label, t } from "../../i18n";
import { until } from "../Schedules";
import Markdown from "../Markdown";
import { confirmDialog, toast } from "../overlay";
import { Panel } from "../ui";
import { Pill } from "./bits";

const field = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

function useAct(reload: () => void) {
  return async (p: Promise<unknown>, done?: string) => {
    try {
      await p;
      if (done) toast(done);
      reload();
      return true;
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
      return false;
    }
  };
}

/** A capability in words: "Nástroj: create task", "Klíč: github-deploy", a permission's label; raw id in Pokročilé. */
export function capLabel(cap: string): string {
  if (cap.startsWith("tool:")) return t("access.cap.tool", { name: cap.slice(5).replace(/_/g, " ") });
  if (cap.startsWith("cred:")) return t("access.cap.cred", { name: cap.slice(5) });
  if (cap.startsWith("outbound:")) return t("access.cap.outbound", { name: cap.slice(9) });
  return label("perm", cap);
}

export function Section({ title, right, children }: { title: string; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col border-b border-line last:border-0">
      <div className="flex flex-wrap items-baseline gap-2 px-4 pt-3 pb-2">
        <h3 className="text-[13px] font-medium">{title}</h3>
        {right && <span className="ml-auto text-xs text-ink-2">{right}</span>}
      </div>
      {children}
    </section>
  );
}

/** One line per limit: what is used against it, until when a temporary one holds. */
function Budgets({ budgets }: { budgets: Record<string, BudgetLine> }) {
  return (
    <div className="grid grid-cols-1 border-t border-line sm:grid-cols-2 md:grid-cols-3">
      {Object.entries(budgets).map(([m, b]) => {
        const pct = b.limit && b.used != null ? Math.min(100, Math.round((100 * b.used) / b.limit)) : null;
        return (
          <div key={m} title={b.reason ? `${b.reason}${b.set_by ? ` — ${b.set_by}` : ""}` : undefined} className="flex flex-col gap-1 border-r border-b border-line px-4 py-2">
            <span className="text-xs text-ink-2">{b.label}</span>
            <span className="font-mono text-sm">
              {b.used != null ? `${fmtMetric(m, b.used)} / ` : ""}
              {b.limit == null ? t("access.no_limit") : fmtMetric(m, b.limit)}
            </span>
            {pct != null && (
              <span className="h-1 rounded bg-line">
                <span className={`block h-1 rounded ${pct >= 100 ? "bg-red-400" : pct >= 80 ? "bg-amber-300" : "bg-accent"}`} style={{ width: `${pct}%` }} />
              </span>
            )}
            {b.expires_at && <span className="text-xs text-amber-300">{t("access.temporary", { until: until(b.expires_at) })}</span>}
          </div>
        );
      })}
    </div>
  );
}

function requestLabel(r: AccessRequest) {
  if (r.what === "capability") return r.capability ? capLabel(r.capability) : "";
  if (r.what === "budget") return `${r.metric ?? ""} ${r.amount ?? t("access.raise")}`;
  return t("access.spike_review");
}

/** Pending requests; the owner can decide any of them (the Access manager usually does). */
function Requests({ items, onDecide }: { items: AccessRequest[]; onDecide: (r: AccessRequest, d: "grant" | "deny") => void }) {
  if (items.length === 0) return <p className="px-4 pb-3 text-sm text-ink-2">{t("access.no_requests")}</p>;
  return (
    <>
      {items.map((r) => (
        <div key={r.id} className="flex flex-col gap-1 border-t border-line px-4 py-2">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-[13px]">{r.agent_name}</span>
            <span className="text-[13px] text-accent" title={r.capability ?? undefined}>
              {requestLabel(r)}
            </span>
            {r.hours ? <Pill>{`${r.hours} h`}</Pill> : null}
            {r.trigger !== "request" && <Pill warn>{t(`access.trigger.${r.trigger}`)}</Pill>}
            {r.needs_owner && <Pill warn>{t("access.owner_only")}</Pill>}
            {r.status === "escalated" && <Pill warn>{t("access.escalated")}</Pill>}
            {r.task_ref && <span className="font-mono text-xs text-ink-2">{r.task_ref}</span>}
            <span className="ml-auto text-xs text-ink-2">{ago(r.created_at)}</span>
            <button className="btn h-7! px-2!" title={t("access.grant_as_asked")} aria-label={t("access.grant_as_asked")} onClick={() => onDecide(r, "grant")}>
              <Check size={13} />
            </button>
            <button className="btn h-7! px-2!" title={t("access.deny")} aria-label={t("access.deny")} onClick={() => onDecide(r, "deny")}>
              <X size={13} />
            </button>
          </span>
          <Markdown text={r.why} compact className="text-xs text-ink-2" />
        </div>
      ))}
    </>
  );
}

/** An audit line in words; no empty `` when the event has nothing to name. */
export function describe(e: AccessEvent): string {
  const d = e.detail as Record<string, string | number | null | undefined>;
  const cap = typeof d.capability === "string" && d.capability ? capLabel(d.capability) : null;
  const what =
    cap ??
    (d.metric ? `${d.metric}${d.amount !== undefined ? ` → ${d.amount ?? t("access.no_limit")}` : ""}` : d.request ? t("access.request_n", { n: d.request }) : "");
  const why = d.reason ?? d.note;
  return [
    `**${label("audit", e.action)}**`,
    what ? ` ${what}` : "",
    d.hours ? ` ${t("access.for_hours", { n: d.hours })}` : "",
    why ? ` — ${why}` : "",
  ].join("");
}

function History({ items }: { items: AccessEvent[] }) {
  if (items.length === 0) return <p className="px-4 pb-3 text-sm text-ink-2">{t("access.no_history")}</p>;
  return (
    <>
      {items.map((e) => (
        <div key={e.id} className="grid grid-cols-[88px_minmax(0,1fr)] gap-2 border-t border-line px-4 py-1.5 text-xs sm:grid-cols-[88px_120px_minmax(0,1fr)]">
          <span className="text-ink-2">{ago(e.at)}</span>
          <span className="hidden truncate text-ink-2 sm:inline">{e.actor_name ?? t("who.platform")}</span>
          <Markdown text={describe(e)} compact />
        </div>
      ))}
    </>
  );
}

/** Grants, budgets, open requests and the decision history of one agent: sections for the merged panel. */
export function AccessSections({ agentId, advanced }: { agentId: number; advanced: boolean }) {
  const [v, setV] = useState<AgentAccess | null>(null);
  const load = useCallback(() => {
    accessApi.agent(agentId).then(setV, () => setV(null));
  }, [agentId]);
  useEffect(load, [load]);
  const act = useAct(load);
  const [cap, setCap] = useState("");
  const [hours, setHours] = useState("");
  const [reason, setReason] = useState("");
  const [metric, setMetric] = useState("usd_day");
  const [amount, setAmount] = useState("");

  if (!v) return null;
  const ask = (title: string) => confirmDialog({ title, confirm: t("act.confirm"), reason: t("access.reason_prompt") }).then((r) => (r ? r : null));
  const grant = (e: FormEvent) => {
    e.preventDefault();
    if (!cap.trim() || !reason.trim()) return;
    act(accessApi.grant(agentId, cap.trim(), reason.trim(), hours ? Number(hours) : null), t("access.granted")).then((ok) => {
      if (!ok) return;
      setCap("");
      setReason("");
      setHours("");
    });
  };
  const budget = async (e: FormEvent) => {
    e.preventDefault();
    const why = await ask(t("access.budget_confirm", { metric: v.budgets[metric]?.label ?? metric, amount: amount || t("access.no_limit") }));
    if (why) act(accessApi.budget(agentId, metric, amount ? Number(amount) : null, why, hours ? Number(hours) : null), t("access.budget_saved"));
  };
  const decide = async (r: AccessRequest, d: "grant" | "deny") => {
    const why = await ask(d === "grant" ? t("access.grant_confirm", { what: requestLabel(r) }) : t("access.deny_confirm", { what: requestLabel(r) }));
    if (why) act(accessApi.decide(r.id, d, why), d === "grant" ? t("access.granted") : t("access.denied"));
  };

  return (
    <>
      <Section title={t("access.grants")} right={v.is_manager ? t("access.grants_manager") : t("access.grants_right")}>
        {!v.managed && <p className="px-4 pb-2 text-xs text-ink-2">{t("access.not_managed")}</p>}
        {v.grants.length === 0 && v.managed && <p className="px-4 pb-2 text-sm text-ink-2">{t("access.no_grants")}</p>}
        {v.grants.map((g) => (
          <div key={g.id} className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-1.5 text-[13px]" title={`${g.reason} — ${g.granted_by_name ?? g.source}`}>
            <span>{capLabel(g.capability)}</span>
            {advanced && <span className="font-mono text-xs text-ink-2">{g.capability}</span>}
            {g.kind === "outbound" && <Pill warn>{t("access.each_send")}</Pill>}
            <span className="truncate text-xs text-ink-2">
              {g.expires_at ? t("access.ends", { until: until(g.expires_at) }) : t("access.permanent")} · {g.granted_by_name ?? g.source}
            </span>
            <button
              className="ml-auto p-1 text-ink-2 hover:text-red-400"
              title={t("access.revoke")}
              aria-label={t("access.revoke_what", { what: capLabel(g.capability) })}
              onClick={async () => {
                const why = await ask(t("access.revoke_confirm", { what: capLabel(g.capability) }));
                if (why) act(accessApi.revoke(g.id, why), t("access.revoked"));
              }}
            >
              <X size={14} />
            </button>
          </div>
        ))}
        {advanced && (
          <form onSubmit={grant} className="flex flex-wrap items-end gap-2 border-t border-line p-3">
            <input aria-label={t("access.capability")} placeholder="tool:create_task · tasks:write · outbound:email.send" value={cap} onChange={(e) => setCap(e.target.value)} className={`${field} w-64 font-mono`} />
            <input aria-label={t("access.hours")} placeholder={t("access.hours_ph")} value={hours} onChange={(e) => setHours(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-44`} />
            <input aria-label={t("access.reason")} placeholder={t("access.reason")} value={reason} onChange={(e) => setReason(e.target.value)} className={`${field} min-w-40 flex-1`} />
            <button className="btn">
              <Plus size={13} /> {t("access.grant")}
            </button>
          </form>
        )}
      </Section>
      <Section title={t("access.budgets")} right={t("access.budgets_right")}>
        <Budgets budgets={v.budgets} />
        <form onSubmit={budget} className="flex flex-wrap items-end gap-2 p-3">
          <select aria-label={t("access.metric")} value={metric} onChange={(e) => setMetric(e.target.value)} className={field}>
            {Object.entries(v.budgets).map(([m, b]) => (
              <option key={m} value={m}>
                {b.label}
              </option>
            ))}
          </select>
          <input aria-label={t("access.amount")} placeholder={t("access.amount_ph")} value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-44`} />
          <input aria-label={t("access.hours")} placeholder={t("access.hours_temp")} value={hours} onChange={(e) => setHours(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-44`} />
          <button className="btn">{t("access.set_limit")}</button>
        </form>
      </Section>
      <Section title={t("access.requests")} right={t("access.open_n", { n: v.requests.length })}>
        <Requests items={v.requests} onDecide={decide} />
      </Section>
      {advanced && (
        <Section title={t("access.history")} right={t("access.history_right")}>
          <div className="max-h-[300px] overflow-y-auto">
            <History items={v.history} />
          </div>
        </Section>
      )}
    </>
  );
}

/** The owner's view on the Access manager's page: the company cap, every open request, the settings. */
export function CompanyAccessPanel() {
  const [c, setC] = useState<CompanyAccess | null>(null);
  const load = useCallback(() => {
    accessApi.company().then(setC, () => setC(null));
  }, []);
  useEffect(load, [load]);
  const act = useAct(load);
  const [metric, setMetric] = useState("usd_day");
  const [amount, setAmount] = useState("");
  if (!c) return null;
  const decide = async (r: AccessRequest, d: "grant" | "deny") => {
    const why = await confirmDialog({
      title: d === "grant" ? t("access.grant_confirm", { what: requestLabel(r) }) : t("access.deny_confirm", { what: requestLabel(r) }),
      confirm: t("act.confirm"),
      reason: t("access.reason_prompt"),
    });
    if (why) act(accessApi.decide(r.id, d, why));
  };
  return (
    <Panel title={t("access.company")} right={[t("access.owner_only"), c.frozen ? t("freeze.frozen_title") : "", c.litellm ? "LiteLLM" : ""].filter(Boolean).join(" · ")}>
      <Section title={t("access.company_cap")}>
        <Budgets budgets={Object.fromEntries(Object.entries(c.budgets).filter(([m]) => m !== "usd_run"))} />
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            const why = await confirmDialog({
              title: t("access.budget_confirm", { metric: c.metrics[metric] ?? metric, amount: amount || t("access.no_limit") }),
              confirm: t("act.confirm"),
              reason: t("access.reason_prompt"),
            });
            if (why) act(accessApi.budget(null, metric, amount ? Number(amount) : null, why, null), t("access.budget_saved"));
          }}
          className="flex flex-wrap items-end gap-2 p-3"
        >
          <select aria-label={t("access.metric")} value={metric} onChange={(e) => setMetric(e.target.value)} className={field}>
            {Object.entries(c.metrics)
              .filter(([m]) => m !== "usd_run")
              .map(([m, l]) => (
                <option key={m} value={m}>
                  {l}
                </option>
              ))}
          </select>
          <input aria-label={t("access.amount")} placeholder={t("access.amount_ph")} value={amount} onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-44`} />
          <button className="btn">{t("access.set_cap")}</button>
        </form>
        <div className="flex flex-wrap items-center gap-3 border-t border-line px-4 py-2 text-xs">
          <span className="text-ink-2">{t("access.spike_watch")}</span>
          {Object.entries(c.settings).map(([k, val]) => (
            <label key={k} className="flex items-center gap-1">
              <span className="text-ink-2">{k.replace(/_/g, " ")}</span>
              <input
                key={`${k}-${val}`}
                aria-label={k}
                defaultValue={val}
                onBlur={(e) => Number(e.target.value) !== val && act(accessApi.settings({ [k]: Number(e.target.value) }), t("act.saved"))}
                className={`${field} w-20`}
              />
            </label>
          ))}
        </div>
      </Section>
      <Section title={t("access.all_requests")} right={t("access.all_requests_right")}>
        <div className="max-h-[340px] overflow-y-auto">
          <Requests items={c.requests} onDecide={decide} />
        </div>
      </Section>
      <Section title={t("access.recent_decisions")}>
        <div className="max-h-[300px] overflow-y-auto">
          <History items={c.history} />
        </div>
      </Section>
    </Panel>
  );
}
