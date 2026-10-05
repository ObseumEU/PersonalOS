import { ArchiveRestore, Check, Eye, FileText, Play, X } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { type Agent, type Approval, agentsApi } from "../agentsApi";
import { confirmDialog, toast } from "../components/overlay";
import { PageHeader, Panel } from "../components/ui";
import { ago, label, t } from "../i18n";
import { TaskLink } from "../taskSheet";

/* HR (pos.hr) on one page: the team's scores from the last daily review, HR's proposals, agents waiting
 * to be admitted over the limit, an admission check, and the archive with restore. */

type Rating = {
  agent_id: string;
  name: string;
  finished: number;
  quality: number | null;
  autonomy: number | null;
  score: number | null;
  cost_per_task: number | null;
};
type Proposal = { kind: string; agent_id: string; agent: string; into: string | null; reason: string; applied: boolean };
type Review = {
  at?: string;
  active_before?: number;
  active_after?: number;
  kpis?: Record<string, number | null>;
  ratings: Rating[];
  proposals: Proposal[];
  task_ids?: string[];
  last_daily?: string | null;
  last_weekly?: string | null;
  report_task_id?: string;
};
type Admit = { allowed: boolean; limit?: string; decision?: string; reason?: string; approval_id?: number };

const post = <T,>(path: string, body: unknown = {}) => api<T>(path, { method: "POST", body: JSON.stringify(body) });
const hrApi = {
  overview: () => api<Review>("/api/hr"),
  review: (apply: boolean) => post<Review>(`/api/hr/review?apply=${apply}`),
  weekly: () => post<Review>("/api/hr/weekly"),
  admit: (name: string, purpose: string, lifetime: string) => post<Admit>("/api/hr/admit", { name, purpose, lifetime }),
  restore: (id: number) => post<{ ok: boolean }>(`/api/hr/agents/${id}/restore`),
  profile: (id: number, purpose: string, lifetime: string) =>
    api<{ ok: boolean }>(`/api/hr/agents/${id}/profile`, { method: "PUT", body: JSON.stringify({ purpose, lifetime }) }),
};

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)} %`);
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const input = "min-w-0 rounded-md border border-line bg-bg px-2.5 py-1.5 text-[13px] outline-none focus:border-accent";
const taskRef = (id: string | number) => `T-${String(id).padStart(3, "0")}`;

function kpiValue(k: string, v: number | null | undefined): string {
  if (v == null) return "—";
  if (k.endsWith("_rate")) return pct(v);
  if (k === "cost_usd") return `$${v.toFixed(2)}`;
  return String(v);
}

function Proposals({ items }: { items: Proposal[] }) {
  if (!items.length) return <p className="px-4 py-4 text-sm text-ink-2">{t("hr.no_proposals")}</p>;
  return (
    <ul>
      {items.map((p, i) => (
        <li key={i} className="flex flex-col gap-0.5 border-b border-line px-4 py-2.5 last:border-0">
          <span className="flex flex-wrap items-baseline gap-x-2 text-[13px]">
            <Link to={`/team/${p.agent_id}`} className="font-medium hover:text-accent">
              {p.agent || p.agent_id}
            </Link>
            <span className="text-ink">{label("hr.kind", p.kind)}</span>
            {p.into && <span className="text-ink-2">{t("hr.into", { name: p.into })}</span>}
            <span className={`ml-auto text-xs ${p.applied ? "text-ink-3" : "text-amber-300"}`}>{p.applied ? t("hr.applied") : t("hr.pending")}</span>
          </span>
          <span className="text-xs text-ink-2">{p.reason}</span>
        </li>
      ))}
    </ul>
  );
}

function ProfileEditor({ agent, onDone }: { agent: Agent; onDone: () => void }) {
  const [purpose, setPurpose] = useState(agent.purpose ?? "");
  const [lifetime, setLifetime] = useState(agent.lifetime ?? "long_lived");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      await hrApi.profile(agent.id, purpose.trim(), lifetime);
      toast(t("hr.profile_saved", { name: agent.name }));
      onDone();
    } catch (err) {
      toast(errText(err), { error: true });
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2 px-4 pb-3">
      <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs text-ink-2">
        {t("hr.profile_purpose")}
        <input required value={purpose} onChange={(e) => setPurpose(e.target.value)} className={input} />
      </label>
      <select value={lifetime} onChange={(e) => setLifetime(e.target.value as Agent["lifetime"] & string)} aria-label={t("hr.admit_lifetime")} className={input}>
        <option value="long_lived">{t("hr.lifetime.long_lived")}</option>
        <option value="one_shot">{t("hr.lifetime.one_shot")}</option>
      </select>
      <button className="btn" disabled={busy || !purpose.trim()}>
        {t("hr.profile_save")}
      </button>
    </form>
  );
}

function Ratings({ ratings, agents, onChanged }: { ratings: Rating[]; agents: Agent[]; onChanged: () => void }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!ratings.length) return <p className="px-4 py-4 text-sm text-ink-2">{t("hr.no_ratings")}</p>;
  const byId = new Map(agents.map((a) => [String(a.id), a]));
  const cell = "px-3 py-2 text-right tabular-nums";
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-[13px]">
        <thead className="text-xs text-ink-2">
          <tr className="border-b border-line">
            <th className="px-4 py-2 text-left font-normal">{t("hr.col.agent")}</th>
            <th className={`${cell} font-normal`}>{t("hr.col.score")}</th>
            <th className={`${cell} font-normal`}>{t("hr.col.finished")}</th>
            <th className={`${cell} font-normal`}>{t("hr.col.quality")}</th>
            <th className={`${cell} font-normal`}>{t("hr.col.autonomy")}</th>
            <th className={`${cell} font-normal`}>{t("hr.col.cost")}</th>
            <th className="px-3 py-2" />
          </tr>
        </thead>
        <tbody>
          {ratings.map((r) => {
            const a = byId.get(String(r.agent_id));
            return (
              <FragmentRow key={r.agent_id} colSpan={7} expanded={!!a && open === a.id} detail={a && <ProfileEditor agent={a} onDone={() => { setOpen(null); onChanged(); }} />}>
                <td className="px-4 py-2">
                  <Link to={`/team/${r.agent_id}`} className="hover:text-accent">
                    {r.name}
                  </Link>
                  {a?.archived && <span className="ml-2 text-xs text-ink-3">{label("agent.status", "archived")}</span>}
                </td>
                <td className={cell}>{r.score == null ? <span className="text-ink-3">{t("hr.not_enough")}</span> : Math.round(r.score * 100)}</td>
                <td className={cell}>{r.finished}</td>
                <td className={cell}>{pct(r.quality)}</td>
                <td className={cell}>{pct(r.autonomy)}</td>
                <td className={cell}>{r.cost_per_task == null ? "—" : `$${r.cost_per_task.toFixed(2)}`}</td>
                <td className="px-3 py-2 text-right">
                  {a && !a.archived && (
                    <button type="button" className="text-xs text-ink-2 hover:text-accent" aria-expanded={open === a.id} onClick={() => setOpen(open === a.id ? null : a.id)}>
                      {t("hr.profile")}
                    </button>
                  )}
                </td>
              </FragmentRow>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FragmentRow({ children, detail, expanded, colSpan }: { children: ReactNode; detail: ReactNode; expanded: boolean; colSpan: number }) {
  return (
    <>
      <tr className="border-b border-line last:border-0">{children}</tr>
      {expanded && (
        <tr className="border-b border-line">
          <td colSpan={colSpan}>{detail}</td>
        </tr>
      )}
    </>
  );
}

function Admissions({ items, onChanged }: { items: Approval[]; onChanged: () => void }) {
  const decide = async (a: Approval, approve: boolean) => {
    try {
      await agentsApi.decide(a.id, approve);
      toast(approve ? t("hr.approved") : t("hr.rejected"));
      onChanged();
    } catch (e) {
      toast(errText(e), { error: true });
    }
  };
  if (!items.length) return <p className="px-4 py-4 text-sm text-ink-2">{t("hr.no_admissions")}</p>;
  return (
    <ul>
      {items.map((a) => {
        const d = a.details as { requested_by?: string; name?: string; purpose?: string; max_active_agents?: number; reason?: string };
        return (
          <li key={a.id} className="flex flex-col gap-2 border-b border-line px-4 py-3 last:border-0">
            <span className="text-[13px]">{t("hr.admission", { who: d.requested_by ?? a.requested_by_name, name: d.name ?? "?", purpose: d.purpose ?? "" })}</span>
            <span className="text-xs text-ink-2">
              {t("hr.admission_limit", { n: d.max_active_agents ?? "?", reason: d.reason ?? "" })} · {ago(a.created_at)}
            </span>
            <span className="flex gap-2">
              <button type="button" className="btn-accent h-8!" onClick={() => decide(a, true)}>
                <Check size={14} /> {t("hr.approve")}
              </button>
              <button type="button" className="btn h-8!" onClick={() => decide(a, false)}>
                <X size={14} /> {t("hr.reject")}
              </button>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function AdmitCheck({ onAsked }: { onAsked: () => void }) {
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [lifetime, setLifetime] = useState("one_shot");
  const [out, setOut] = useState<Admit | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await hrApi.admit(name.trim(), purpose.trim(), lifetime);
      setOut(r);
      if (r.approval_id) onAsked();
    } catch (err) {
      toast(errText(err), { error: true });
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 px-4 py-3">
      <p className="text-xs leading-relaxed text-ink-2">{t("hr.admit_hint")}</p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-xs text-ink-2">
          {t("hr.admit_name")}
          <input required value={name} onChange={(e) => setName(e.target.value)} className={`${input} w-40`} />
        </label>
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs text-ink-2">
          {t("hr.admit_purpose")}
          <input required value={purpose} onChange={(e) => setPurpose(e.target.value)} className={input} />
        </label>
        <label className="flex flex-col gap-1 text-xs text-ink-2">
          {t("hr.admit_lifetime")}
          <select value={lifetime} onChange={(e) => setLifetime(e.target.value)} className={input}>
            <option value="one_shot">{t("hr.lifetime.one_shot")}</option>
            <option value="long_lived">{t("hr.lifetime.long_lived")}</option>
          </select>
        </label>
        <button className="btn" disabled={busy || !name.trim() || !purpose.trim()}>
          {t("hr.admit_check")}
        </button>
      </div>
      {out && (
        <p className={`text-xs ${out.allowed ? "text-emerald-300" : "text-amber-300"}`} role="status">
          {out.allowed && !out.decision ? t("hr.admit_ok") : t("hr.admit_no", { limit: out.limit ?? "", decision: out.decision ?? "", reason: out.reason ?? "" })}
          {out.approval_id ? ` ${t("hr.admit_asked")}` : ""}
        </p>
      )}
    </form>
  );
}

/** HR: /hr, linked from Tým. */
export default function HR() {
  const [review, setReview] = useState<Review | null>(null);
  const [preview, setPreview] = useState<Review | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [admissions, setAdmissions] = useState<Approval[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    hrApi.overview().then(
      (r) => {
        setReview(r);
        setError(null);
      },
      (e) => setError(errText(e)),
    );
    agentsApi.list().then((x) => setAgents(x.agents), () => undefined);
    agentsApi.approvals("pending").then((xs) => setAdmissions(xs.filter((a) => a.action === "raise_agent_limit")), () => undefined);
  }, []);
  useEffect(load, [load]);

  const step = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    try {
      await fn();
    } catch (e) {
      toast(errText(e), { error: true });
    } finally {
      setBusy(null);
    }
  };
  const runReview = async () => {
    const ok = await confirmDialog({ title: t("hr.run_ask"), body: t("hr.run_body"), confirm: t("hr.run") });
    if (ok === null) return;
    await step("run", async () => {
      const r = await hrApi.review(true);
      toast(t("hr.ran", { before: r.active_before ?? "?", after: r.active_after ?? "?" }));
      setPreview(null);
      load();
    });
  };
  const restore = async (a: Agent) => {
    const ok = await confirmDialog({ title: t("hr.restore_ask", { name: a.name }), body: t("hr.restore_body"), confirm: t("hr.restore") });
    if (ok === null) return;
    await step(`r${a.id}`, async () => {
      await hrApi.restore(a.id);
      toast(t("hr.restored", { name: a.name }));
      load();
    });
  };

  const archived = agents.filter((a) => a.archived && a.kind !== "human");
  const kpis = review?.kpis ?? {};
  const head = review?.last_daily
    ? [t("hr.last_daily", { ago: ago(review.last_daily) }), review.last_weekly ? t("hr.last_weekly", { ago: ago(review.last_weekly) }) : null].filter(Boolean).join(" · ")
    : t("hr.never");

  return (
    <div className="flex flex-col gap-4">
      <PageHeader kicker={t("hr.kicker")} title={t("hr.title")} sub={t("hr.sub")} />
      <div className="flex flex-wrap items-center gap-2">
        <Link to="/team" className="text-[13px] text-ink-2 hover:text-accent">
          {t("hr.back")}
        </Link>
        <span className="text-xs text-ink-2">· {head}</span>
        <span className="ml-auto flex flex-wrap gap-2">
          <button type="button" className="btn" disabled={!!busy} onClick={() => step("preview", async () => setPreview(await hrApi.review(false)))}>
            <Eye size={14} /> {t("hr.preview")}
          </button>
          <button
            type="button"
            className="btn"
            disabled={!!busy}
            onClick={() =>
              step("weekly", async () => {
                const r = await hrApi.weekly();
                toast(t("hr.weekly_done", { ref: r.report_task_id ? taskRef(r.report_task_id) : "—" }));
                load();
              })
            }
          >
            <FileText size={14} /> {t("hr.weekly")}
          </button>
          <button type="button" className="btn-accent" disabled={!!busy} onClick={runReview}>
            <Play size={14} /> {t("hr.run")}
          </button>
        </span>
      </div>
      {error && <p className="text-sm text-red-400">{t("hr.error", { error })}</p>}
      {!review && !error && <p className="text-sm text-ink-2">{t("hr.loading")}</p>}

      {review && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <div className="flex min-w-0 flex-col gap-4 lg:col-span-7">
            <Panel title={t("hr.kpis")}>
              <div className="grid grid-cols-2 sm:grid-cols-5">
                {["tasks_completed", "unassisted_rate", "returned_rate", "owner_interventions", "cost_usd"].map((k) => (
                  <div key={k} className="flex flex-col gap-1 border-b border-line px-4 py-3 sm:border-b-0">
                    <span className="text-xs text-ink-2">{t(`hr.kpi.${k}`)}</span>
                    <span className="font-mono text-lg tabular-nums">{kpiValue(k, kpis[k])}</span>
                  </div>
                ))}
              </div>
            </Panel>
            <Panel title={t("hr.ratings")}>
              <Ratings ratings={review.ratings} agents={agents} onChanged={load} />
            </Panel>
          </div>
          <div className="flex min-w-0 flex-col gap-4 lg:col-span-5">
            <Panel title={t("hr.admissions")} right={admissions.length ? String(admissions.length) : undefined}>
              <Admissions items={admissions} onChanged={load} />
            </Panel>
            {preview && (
              <Panel title={t("hr.preview_title")} right={<button aria-label={t("act.close")} onClick={() => setPreview(null)}><X size={13} /></button>}>
                <Proposals items={preview.proposals} />
              </Panel>
            )}
            <Panel title={t("hr.proposals")}>
              <Proposals items={review.proposals} />
              {(review.task_ids ?? []).length > 0 && (
                <p className="flex flex-wrap gap-2 border-t border-line px-4 py-2 text-xs text-ink-2">
                  {(review.task_ids ?? []).map((id) => (
                    <TaskLink key={id} taskRef={taskRef(id)} className="font-mono hover:text-accent">
                      {taskRef(id)}
                    </TaskLink>
                  ))}
                </p>
              )}
            </Panel>
            <Panel title={t("hr.admit")}>
              <AdmitCheck onAsked={load} />
            </Panel>
            <Panel title={t("hr.archived")} right={archived.length ? String(archived.length) : undefined}>
              {archived.length === 0 ? (
                <p className="px-4 py-4 text-sm text-ink-2">{t("hr.no_archived")}</p>
              ) : (
                <ul>
                  {archived.map((a) => (
                    <li key={a.id} className="flex items-center gap-3 border-b border-line px-4 py-2.5 last:border-0">
                      <span className="flex min-w-0 flex-1 flex-col">
                        <Link to={`/team/${a.id}`} className="truncate text-[13px] hover:text-accent">
                          {a.name}
                        </Link>
                        {a.purpose && <span className="truncate text-xs text-ink-2">{a.purpose}</span>}
                      </span>
                      <button type="button" className="btn h-8!" disabled={busy === `r${a.id}`} onClick={() => restore(a)}>
                        <ArchiveRestore size={14} /> {t("hr.restore")}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>
        </div>
      )}
    </div>
  );
}
