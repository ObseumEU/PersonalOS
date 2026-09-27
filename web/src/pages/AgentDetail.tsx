import { KeyRound, Pause, Pencil, Play, RotateCcw, Send, Square, UserCheck } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { type AgentDetail as Detail, agentsApi, type Org, type TraceEntry } from "../agentsApi";
import { markdownSnippet } from "../markdownText";
import { AccessSections, CompanyAccessPanel, Section } from "../components/agents/AccessPanel";
import { AgentCredentialsPanel } from "./Credentials";
import { ActorChip, EngineBadge, Pill, StatusDot } from "../components/agents/bits";
import { WorkingOnText, workingOn } from "../components/agents/WorkingOn";
import { StatePill } from "../components/tasks/bits";
import { FeedbackPanel, InstructionsEditor } from "../components/Feedback";
import Markdown from "../components/Markdown";
import { confirmDialog, toast } from "../components/overlay";
import { SchedulesPanel } from "../components/Schedules";
import { PageHeader, Panel } from "../components/ui";
import { ago, fmtTime, label, t } from "../i18n";
import { tasksApi } from "../tasksApi";
import { fmtTokens } from "./Agents";

type Priority = "fyi" | "change_plan" | "stop";
type Message = {
  id: number;
  from_actor: number;
  to_actor: number;
  from_name: string;
  to_name: string;
  body: string;
  priority: Priority;
  created_at: string;
  read_at: string | null;
  acked_at: string | null;
};
type Hr = { rating: { score: number | null; finished: number; quality: number | null; autonomy: number | null } | null; proposals: { kind: string; reason: string }[] };
type Full = Detail & { hr?: Hr };

const PRIORITY_CLS: Record<Priority, string> = { fyi: "text-ink-2", change_plan: "text-accent", stop: "text-amber-300" };
const TABS = ["overview", "activity", "access", "instructions"] as const;
type Tab = (typeof TABS)[number];
const TAB_KEY: Record<Tab, string> = {
  overview: "agent.tab.overview",
  activity: "agent.tab.activity",
  access: "agent.tab.access",
  instructions: "agent.tab.instructions",
};

function Inject({ agentId, current, onSent }: { agentId: number; current?: string; onSent: () => void }) {
  const [body, setBody] = useState("");
  const [priority, setPriority] = useState<Priority>("change_plan");
  const send = (e: FormEvent) => {
    e.preventDefault();
    if (!body.trim()) return;
    api(`/api/agents/${agentId}/message`, { method: "POST", body: JSON.stringify({ body, priority, task_id: current }) }).then(
      () => {
        setBody("");
        toast(t("agent.msg_sent"));
        onSent();
      },
      (err) => toast(err.message, { error: true }),
    );
  };
  return (
    <form onSubmit={send} className="flex flex-col gap-2 border-t border-line p-3">
      <span className="text-xs text-ink-2">
        {t("agent.inject")} · {t("agent.inject_hint")}
      </span>
      <textarea
        rows={2}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        aria-label={t("agent.inject")}
        placeholder={t("agent.inject_placeholder")}
        className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
      />
      <div className="flex flex-wrap items-center gap-3">
        {(["fyi", "change_plan", "stop"] as const).map((p) => (
          <label key={p} className={`flex items-center gap-1.5 text-xs ${PRIORITY_CLS[p]}`}>
            <input type="radio" name="priority" className="accent-accent" checked={priority === p} onChange={() => setPriority(p)} />
            {t(`priority.${p}`)}
          </label>
        ))}
        <button className="btn-accent ml-auto">
          <Send size={13} /> {t("act.send")}
        </button>
      </div>
    </form>
  );
}

function EditButtons({ editing, onEdit, onSave, onCancel, disabled }: { editing: boolean; onEdit: () => void; onSave: () => void; onCancel: () => void; disabled?: boolean }) {
  return editing ? (
    <span className="flex gap-2">
      <button className="btn-accent h-7!" onClick={onSave} disabled={disabled}>
        {t("act.save")}
      </button>
      <button className="btn h-7!" onClick={onCancel}>
        {t("act.cancel")}
      </button>
    </span>
  ) : (
    <button className="btn h-7!" onClick={onEdit}>
      <Pencil size={13} /> {t("act.edit")}
    </button>
  );
}

/** Role, team and manager: read-only until Upravit; only the owner may change them (the API enforces it). */
function PlacePanel({ a, onSaved }: { a: Full; onSaved: () => void }) {
  const [org, setOrg] = useState<Org | null>(null);
  const [editing, setEditing] = useState(false);
  const initial = { role: a.role ?? "", team: a.team ?? "", reports_to: a.reports_to ?? (null as number | null) };
  const [form, setForm] = useState(initial);
  useEffect(() => {
    agentsApi.org().then(setOrg, () => undefined);
  }, [a.id]);
  const managers = (org?.members ?? []).filter((m) => m.id !== a.id);
  const field = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";
  const save = async () => {
    const before = a.is_owner ? { role: a.role, team: a.team } : { role: a.role, team: a.team, reports_to: a.reports_to };
    const next = a.is_owner ? { role: form.role || null, team: form.team || null } : { role: form.role || null, team: form.team || null, reports_to: form.reports_to };
    try {
      await agentsApi.setOrg(a.id, next);
      setEditing(false);
      onSaved();
      toast(t("agent.org_saved"), { undo: () => agentsApi.setOrg(a.id, before).then(onSaved) });
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  };
  const bossName = a.reports_to_name ?? t("agent.boss_default");
  return (
    <Panel
      title={t("agent.place")}
      right={
        <EditButtons
          editing={editing}
          onEdit={() => setEditing(true)}
          onSave={save}
          onCancel={() => {
            setForm(initial);
            setEditing(false);
          }}
        />
      }
    >
      <div className="flex flex-wrap items-end gap-4 p-4">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-2">{t("agent.role")}</span>
          {editing ? (
            <>
              <input list="org-roles" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className={`${field} w-44`} />
              <datalist id="org-roles">
                {org?.roles.map((r) => <option key={r} value={r} />)}
              </datalist>
            </>
          ) : (
            <span className="text-sm">{a.role ? a.role.replace(/_/g, " ") : "—"}</span>
          )}
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-2">{t("agent.team_field")}</span>
          {editing ? (
            <input value={form.team} onChange={(e) => setForm({ ...form, team: e.target.value })} className={`${field} w-40`} />
          ) : (
            <span className="text-sm">{a.team ?? "—"}</span>
          )}
        </label>
        {!a.is_owner && (
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-2">{t("agent.boss")}</span>
            {editing ? (
              <select value={form.reports_to ?? ""} onChange={(e) => setForm({ ...form, reports_to: e.target.value ? Number(e.target.value) : null })} className={`${field} w-48`}>
                <option value="">{t("agent.boss_default")}</option>
                {managers.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.is_owner ? t("agent.you_owner") : m.name}
                  </option>
                ))}
              </select>
            ) : a.reports_to ? (
              <Link to={`/team/${a.reports_to}`} className="text-sm hover:text-accent">
                {bossName}
              </Link>
            ) : (
              <span className="text-sm">{bossName}</span>
            )}
          </label>
        )}
        <Link to="/team?tab=structure" className="btn ml-auto">
          {t("agent.structure")} →
        </Link>
      </div>
    </Panel>
  );
}

/** Runtime and model: read-only until Upravit. */
function EnginePanel({ a, onSaved }: { a: Full; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [engine, setEngine] = useState(a.engine ?? "");
  const [model, setModel] = useState(a.model ?? "");
  const field = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";
  if (a.is_owner) return null;
  const save = async () => {
    const before = { engine: a.engine, model: a.model };
    try {
      await agentsApi.setEngine(a.id, engine || null, model || null);
      setEditing(false);
      onSaved();
      toast(t("agent.engine_saved"), { undo: () => agentsApi.setEngine(a.id, before.engine, before.model).then(onSaved) });
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  };
  return (
    <Panel
      title={t("agent.engine")}
      right={
        <EditButtons
          editing={editing}
          onEdit={() => setEditing(true)}
          onSave={save}
          onCancel={() => {
            setEngine(a.engine ?? "");
            setModel(a.model ?? "");
            setEditing(false);
          }}
        />
      }
    >
      <div className="flex flex-wrap items-end gap-4 p-4">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-2">{t("agent.engine")}</span>
          {editing ? (
            <select className={field} value={engine} onChange={(e) => setEngine(e.target.value)}>
              <option value="">{t("engine.default")}</option>
              <option value="claude">{t("engine.claude")}</option>
              <option value="codex">{t("engine.codex")}</option>
              <option value="auto">{t("engine.auto")}</option>
            </select>
          ) : (
            <span className="text-sm">{a.engine ? t(`engine.${a.engine}`) : t("engine.default")}</span>
          )}
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-ink-2">{t("agent.model")}</span>
          {editing ? (
            <input placeholder="claude-opus-5-5" value={model} onChange={(e) => setModel(e.target.value)} className={`${field} w-48 font-mono`} />
          ) : (
            <span className="font-mono text-sm">{a.model ?? "claude-opus-5-5"}</span>
          )}
        </label>
        <EngineBadge view={a.engine_view} engine={a.engine_effective} model={a.engine_effective !== "codex" ? a.model ?? "claude-opus-5-5" : null} />
      </div>
    </Panel>
  );
}

/** What the agent may do: labels by default; checkboxes only in Upravit, saved with Uložit, undo in the toast. */
function Permissions({ a, perms, advanced, onSaved }: { a: Full; perms: Record<string, string>; advanced: boolean; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [chosen, setChosen] = useState<string[]>(a.permissions);
  const all = a.permissions.includes("*");
  const save = async () => {
    const before = a.permissions;
    try {
      await agentsApi.setPermissions(a.id, chosen);
      setEditing(false);
      onSaved();
      toast(t("agent.permissions_saved"), {
        undo: () =>
          agentsApi.setPermissions(a.id, before).then(() => {
            onSaved();
            toast(t("agent.permissions_undone"));
          }),
      });
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  };
  return (
    <Section
      title={t("agent.permissions")}
      right={
        a.is_owner ? (
          t("perm.*")
        ) : (
          <EditButtons
            editing={editing}
            onEdit={() => {
              setChosen(a.permissions);
              setEditing(true);
            }}
            onSave={save}
            onCancel={() => {
              setChosen(a.permissions);
              setEditing(false);
            }}
          />
        )
      }
    >
      {editing ? (
        <div className="grid grid-cols-1 gap-x-4 gap-y-2 px-4 pb-3 sm:grid-cols-2">
          {Object.entries(perms).map(([p, why]) => (
            <label key={p} title={why} className="flex items-center gap-2 text-[13px]">
              <input
                type="checkbox"
                className="accent-accent"
                checked={chosen.includes(p) || all}
                disabled={all}
                onChange={(e) => setChosen(e.target.checked ? [...chosen, p] : chosen.filter((x) => x !== p))}
              />
              {label("perm", p)}
              {advanced && <span className="font-mono text-xs text-ink-2">{p}</span>}
            </label>
          ))}
        </div>
      ) : (
        <ul className="flex flex-wrap gap-1.5 px-4 pb-3">
          {(all ? ["*"] : a.permissions).map((p) => (
            <li key={p} className="rounded border border-line px-2 py-0.5 text-[13px]">
              {label("perm", p)}
              {advanced && <span className="ml-1.5 font-mono text-xs text-ink-2">{p}</span>}
            </li>
          ))}
          {a.permissions.length === 0 && <li className="text-sm text-ink-2">—</li>}
        </ul>
      )}
    </Section>
  );
}

/** A trace line in words: "napsal do chatu · task 12", not "mcp:chat_send task 12 {}". */
function traceText(e: TraceEntry) {
  const act = e.action.replace(/^mcp:/, "");
  const detail = Object.entries(e.detail ?? {})
    .filter(([, v]) => v !== null && v !== "" && typeof v !== "object")
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
  return { act: label("audit", act), raw: act, what: [e.entity ? `${e.entity} ${e.entity_id ?? ""}`.trim() : "", detail].filter(Boolean).join(" · ") };
}

function Overview({ a, messages, onChange }: { a: Full; messages: Message[]; onChange: () => void }) {
  const current = a.queue.find((x) => x.status === "working");
  const w = workingOn(a) ?? (current ? { task_ref: current.ref, since: null, title: current.title } : null);
  const lastResult = a.queue.find((x) => x.status === "review" && x.progress_note);
  const lastRun = a.runs.find((r) => r.status !== "running" && r.detail);
  const unread = messages.filter((m) => m.to_actor === a.id && !m.read_at).length;
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <div className="flex min-w-0 flex-col gap-4 lg:col-span-7">
        <Panel title={w ? t("agent.working_now") : t("agent.not_running")} bodyClassName="flex flex-col">
          <div className="flex flex-col gap-3 px-4 py-3">
            {w ? (
              <p className="text-sm">
                <WorkingOnText w={w} withTitle />
                {current?.progress != null && <span className="text-ink-2"> · {t("agent.progress", { n: current.progress })}</span>}
              </p>
            ) : (
              <p className="text-sm text-ink-2">{a.paused ? t("agents.paused_by_owner") : t("working.none")}</p>
            )}
            {current?.definition_of_done && (
              <p className="text-[13px]">
                <span className="text-xs text-ink-2">{t("agent.done_when")}: </span>
                {current.definition_of_done}
              </p>
            )}
            {(a.pending_gates ?? []).length > 0 && (
              <div className="flex flex-col gap-1">
                <span className="text-xs text-amber-300">{t("agent.gate")}</span>
                {a.pending_gates!.map((g) => (
                  <Link key={`${g.kind}-${g.id}`} to={g.link} className="text-[13px] text-amber-300 hover:underline">
                    {g.kind === "approval" ? t("agent.gate_approval", { title: g.title }) : t("agent.gate_ask", { title: g.title })} →
                  </Link>
                ))}
              </div>
            )}
          </div>
          {a.kind !== "human" && <Inject agentId={a.id} current={current?.ref} onSent={onChange} />}
        </Panel>

        <Panel title={t("agent.queue")} right={t("agent.queue_right", { next: a.queued, review: a.review })} bodyClassName="max-h-[360px] overflow-y-auto">
          {a.queue.length === 0 && <p className="px-4 py-3 text-sm text-ink-2">{t("agent.no_tasks")}</p>}
          {a.queue.map((x) => (
            <Link
              key={x.id}
              to={`/tasks?view=agents&task=${x.ref}`}
              className={`flex flex-col gap-1 border-b border-line px-4 py-2.5 last:border-0 hover:bg-raised ${x.status === "working" ? "bg-raised shadow-[inset_2px_0_0_var(--color-accent)]" : ""}`}
            >
              <span className="flex items-center gap-2">
                <span className="font-mono text-xs text-ink-2">{x.ref}</span>
                <StatePill task={x} />
              </span>
              <span className="text-[13px]">{x.title}</span>
            </Link>
          ))}
        </Panel>
      </div>

      <div className="flex min-w-0 flex-col gap-4 lg:col-span-5">
        <Panel title={t("agent.last_result")}>
          <div className="px-4 py-3">
            {lastResult ? (
              <>
                <Link to={`/tasks?task=${lastResult.ref}`} className="text-sm hover:text-accent">
                  <span className="font-mono text-xs text-ink-2">{lastResult.ref}</span> {lastResult.title}
                </Link>
                <Markdown text={markdownSnippet(lastResult.progress_note ?? "", 400)} compact className="mt-1 text-ink-2" />
              </>
            ) : lastRun ? (
              <p className="text-[13px] text-ink-2">
                {label("run", lastRun.kind)} · {ago(lastRun.started_at)} — {lastRun.detail.slice(0, 240)}
              </p>
            ) : (
              <p className="text-sm text-ink-2">{t("agent.no_result")}</p>
            )}
          </div>
        </Panel>
        <Panel
          title={t("agent.memory_preview")}
          right={
            a.memory.length > 0 ? (
              <Link to="?tab=instructions" className="hover:text-accent">
                {t("agent.memory_more")} →
              </Link>
            ) : undefined
          }
        >
          {a.memory.length === 0 && <p className="px-4 py-3 text-sm text-ink-2">{t("agent.no_memory")}</p>}
          {a.memory.slice(0, 3).map((m) => (
            <p key={m.id} className="border-b border-line px-4 py-2 text-[13px] text-ink-2 last:border-0">
              {markdownSnippet(m.body, 200)}
            </p>
          ))}
        </Panel>
        <Panel
          title={t("agent.messages")}
          right={
            <span className="flex items-center gap-2">
              {t("agent.unread", { n: unread })}
              <Link to={`/chat?dm=${a.id}`} className="text-accent">
                {t("agent.open_dm")} →
              </Link>
            </span>
          }
          bodyClassName="max-h-[240px] overflow-y-auto"
        >
          {messages.length === 0 && <p className="px-4 py-3 text-sm text-ink-2">{t("agent.no_messages")}</p>}
          {messages.slice(0, 20).map((m) => (
            <div key={m.id} className="flex flex-col gap-0.5 border-b border-line px-4 py-2 last:border-0">
              <span className="text-xs text-ink-2">
                {m.from_name} → {m.to_name} · <span className={PRIORITY_CLS[m.priority]}>{t(`priority.${m.priority}`)}</span> · {ago(m.created_at)} ·{" "}
                {m.acked_at ? t("agent.msg.acted") : m.read_at ? t("agent.msg.read") : t("agent.msg.unread")}
              </span>
              <span className="text-[13px]">{m.body}</span>
            </div>
          ))}
        </Panel>
        <Panel title={t("agent.week")}>
          <div className="grid grid-cols-3 border-b border-line">
            {(
              [
                ["agent.week.done", a.week.done],
                ["agent.week.returned", a.week.returned],
                ["agent.week.interventions", a.week.interventions],
              ] as const
            ).map(([k, v]) => (
              <div key={k} className="flex flex-col px-4 py-2">
                <span className="text-xs text-ink-2">{t(k)}</span>
                <span className="font-mono text-lg">{v}</span>
              </div>
            ))}
          </div>
          <p className="px-4 py-2 text-xs text-ink-2">
            {t("agent.hr_score", { v: a.hr?.rating?.score == null ? t("agents.hr_not_enough") : `${Math.round(a.hr.rating.score * 100)} / 100` })}
          </p>
        </Panel>
      </div>
    </div>
  );
}

function Activity({ a, advanced, onChange }: { a: Full; advanced: boolean; onChange: () => void }) {
  const rollback = async (id: number) => {
    const ok = await confirmDialog({ title: t("agent.rollback_confirm", { id }), body: t("agent.rollback_body"), confirm: t("act.confirm"), danger: true });
    if (ok === null) return;
    api(`/api/runs/${id}/rollback`, { method: "POST" }).then(
      () => {
        toast(t("agent.rolled_back", { id }));
        onChange();
      },
      (e) => toast(e.message, { error: true }),
    );
  };
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <Panel title={t("agent.runs")} right={t("agent.runs_right")} className="min-w-0 lg:col-span-6" bodyClassName="max-h-[560px] overflow-y-auto">
        {a.runs.length === 0 && <p className="px-4 py-3 text-sm text-ink-2">{t("agent.no_runs")}</p>}
        {a.runs.map((r) => (
          <div key={r.id} className="flex flex-col gap-0.5 border-b border-line px-4 py-2 last:border-0">
            <span className="flex flex-wrap items-center gap-2 text-[13px]">
              <span className={r.status === "ok" || r.status === "running" ? "text-accent" : "text-ink-2"}>{label("runstatus", r.status)}</span>
              <span>{label("run", r.kind)}</span>
              {advanced && (
                <span className="font-mono text-xs text-ink-2">
                  #{r.id} {r.kind}
                </span>
              )}
              <span className="text-xs text-ink-2">{ago(r.started_at)}</span>
              <span className="ml-auto font-mono text-xs text-ink-2">{r.input_tokens != null ? `${fmtTokens((r.input_tokens ?? 0) + (r.output_tokens ?? 0))} tok` : ""}</span>
              <button title={t("agent.rollback", { id: r.id })} aria-label={t("agent.rollback", { id: r.id })} className="p-1 text-ink-2 hover:text-accent" onClick={() => rollback(r.id)}>
                <RotateCcw size={14} />
              </button>
            </span>
            {(r.detail || r.tool_calls != null) && (
              <span className="truncate text-xs text-ink-2">
                {r.tool_calls != null ? t("agent.tool_calls", { n: r.tool_calls }) : ""}
                {r.detail ? `${r.tool_calls != null ? " · " : ""}${r.detail.slice(0, 160)}` : ""}
              </span>
            )}
          </div>
        ))}
      </Panel>
      <Panel title={t("agent.trace")} className="min-w-0 lg:col-span-6" bodyClassName="max-h-[560px] overflow-y-auto">
        {a.trace.length === 0 && <p className="px-4 py-3 text-sm text-ink-2">{t("agent.no_trace")}</p>}
        {a.trace.map((e) => {
          const x = traceText(e);
          return (
            <div key={e.id} className="grid grid-cols-[48px_minmax(0,1fr)] gap-2.5 border-b border-line px-4 py-1.5 text-xs last:border-0">
              <span className="font-mono text-ink-2">{fmtTime(e.at)}</span>
              <span className="min-w-0">
                <span className="text-accent">{x.act}</span>
                {advanced && x.raw !== x.act && <span className="ml-1.5 font-mono text-ink-2">{x.raw}</span>}
                {x.what && <span className="block truncate text-ink-2">{x.what}</span>}
              </span>
            </div>
          );
        })}
      </Panel>
    </div>
  );
}

export default function AgentDetail() {
  const id = Number(useParams().id);
  const [params, setParams] = useSearchParams();
  const tab: Tab = (TABS as readonly string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : "overview";
  const [a, setA] = useState<Full | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [perms, setPerms] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);

  const load = useCallback(() => {
    agentsApi.get(id).then(setA, (e) => setError(e.message));
    api<Message[]>(`/api/agents/${id}/messages`).then(setMessages, () => setMessages([]));
  }, [id]);
  useEffect(() => {
    load();
    agentsApi.list().then((d) => setPerms(d.permissions), () => undefined);
    const h = setInterval(load, 5000); // live run view
    return () => clearInterval(h);
  }, [load]);

  async function act(p: Promise<unknown>, done?: string) {
    try {
      const out = (await p) as { api_key?: string };
      if (out?.api_key) toast(t("agent.new_key_shown", { key: out.api_key }));
      else if (done) toast(done);
      load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  }

  if (!a) return <p className="p-6 text-sm text-ink-2">{error ?? t("act.loading")}</p>;
  const current = a.queue.find((x) => x.status === "working");

  const stop = async () => {
    const ok = await confirmDialog({ title: t("agent.stop_confirm", { name: a.name }), body: t("agent.stop_body"), confirm: t("act.stop"), danger: true });
    if (ok !== null) act(agentsApi.action(id, "stop"), t("agent.stopped", { name: a.name }));
  };
  const archive = async () => {
    const ok = await confirmDialog({ title: t("agent.archive_confirm", { name: a.name }), body: t("agent.archive_body"), confirm: t("act.archive"), danger: true });
    if (ok !== null) act(agentsApi.action(id, "archive"), t("agent.archived", { name: a.name }));
  };
  const newKey = async () => {
    const ok = await confirmDialog({ title: t("agent.new_key_confirm"), body: t("agent.new_key_body"), confirm: t("agent.new_key"), danger: true });
    if (ok !== null) act(api(`/api/agents/${id}/key`, { method: "POST" }));
  };

  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={t("agent.kicker", { name: a.name })} title={a.name} sub={a.purpose ?? t("agent.builtin")} />
      <div className="flex flex-wrap items-center gap-2">
        <ActorChip a={a} />
        <StatusDot status={a.status} />
        {a.system && <Pill>{t("agents.system")}</Pill>}
        {a.role && <Pill>{a.role.replace(/_/g, " ")}</Pill>}
        {a.team && <span className="text-xs text-ink-2">{t("agent.team", { team: a.team })}</span>}
        {a.reports_to_name && (
          <Link to={`/team/${a.reports_to}`} className="text-xs text-ink-2 hover:text-accent">
            {t("agent.reports_to", { name: a.reports_to_name })}
          </Link>
        )}
        {a.kind !== "human" && <EngineBadge view={a.engine_view} engine={a.engine_effective} model={a.engine_effective !== "codex" ? a.model ?? "claude-opus-5-5" : null} />}
        <span className="text-xs text-ink-2">{t("agent.last_seen", { ago: ago(a.last_seen_at) })}</span>
        {a.kind !== "human" && (
          <span className="flex flex-wrap gap-2 sm:ml-auto">
            {a.paused ? (
              <button className="btn" onClick={() => act(agentsApi.action(id, "resume"), t("agent.resumed", { name: a.name }))}>
                <Play size={13} /> {t("act.resume")}
              </button>
            ) : (
              <button className="btn" onClick={() => act(agentsApi.action(id, "pause"), t("agent.paused", { name: a.name }))}>
                <Pause size={13} /> {t("act.pause")}
              </button>
            )}
            {current && (
              <button className="btn" title={t("agent.take_over_title")} onClick={() => act(tasksApi.update(current.ref, { assignee: "me" }), t("agent.took_over", { ref: current.ref }))}>
                <UserCheck size={13} /> {t("agent.take_over")}
              </button>
            )}
            <button className="btn border-amber-400/60! text-amber-300!" onClick={stop}>
              <Square size={13} /> {t("act.stop")}
            </button>
            {a.archived ? (
              <button className="btn" onClick={() => act(agentsApi.action(id, "restore"), t("agent.restored", { name: a.name }))}>
                {t("act.restore")}
              </button>
            ) : (
              !a.system && (
                <button className="btn" onClick={archive}>
                  {t("act.archive")}
                </button>
              )
            )}
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-end gap-2 border-b border-line">
        <nav className="-mx-1 flex min-w-0 flex-1 gap-1 overflow-x-auto px-1" role="tablist" aria-label={a.name}>
          {TABS.map((x) => (
            <button
              key={x}
              role="tab"
              aria-selected={x === tab}
              onClick={() => setParams(x === "overview" ? {} : { tab: x }, { replace: true })}
              className={`-mb-px shrink-0 border-b-2 px-3 py-2 text-sm whitespace-nowrap ${x === tab ? "border-accent text-ink" : "border-transparent text-ink-2 hover:text-ink"}`}
            >
              {t(TAB_KEY[x])}
            </button>
          ))}
        </nav>
        {(tab === "activity" || tab === "access") && (
          <label className="mb-2 flex items-center gap-1.5 text-xs text-ink-2">
            <input type="checkbox" className="accent-accent" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} />
            {t("act.advanced")}
          </label>
        )}
      </div>

      {tab === "overview" && <Overview a={a} messages={messages} onChange={load} />}
      {tab === "activity" && <Activity a={a} advanced={advanced} onChange={load} />}
      {tab === "access" && (
        <div className="flex flex-col gap-4">
          <Panel title={t("access.panel")}>
            <Permissions key={a.permissions.join(",")} a={a} perms={perms} advanced={advanced} onSaved={load} />
            <Section title={t("agent.tokens")} right={label("agents.budget", a.budget_class ?? "normal")}>
              <div className="grid grid-cols-2 border-t border-line sm:grid-cols-4">
                {[
                  ["agent.tokens.24h", fmtTokens(a.tokens_24h)],
                  ["agent.tokens.7d", fmtTokens(a.tokens_7d)],
                  ["agent.tokens.cap", a.daily_cap ? fmtTokens(a.daily_cap) : t("agent.none")],
                  ["agent.tokens.runs", String(a.runs.length)],
                ].map(([k, v]) => (
                  <div key={k} className="flex flex-col border-r border-b border-line px-4 py-2">
                    <span className="text-xs text-ink-2">{t(k)}</span>
                    <span className="font-mono text-lg">{v}</span>
                  </div>
                ))}
              </div>
            </Section>
            {a.kind !== "human" && <AccessSections agentId={a.id} advanced={advanced} />}
            {a.kind !== "human" && (
              <section className="flex flex-col gap-2 border-t border-line p-4">
                <h3 className="flex items-center gap-2 text-[13px] font-medium">
                  <KeyRound size={14} className="text-ink-2" /> {t("access.keys")}
                </h3>
                <AgentCredentialsPanel agentId={a.id} />
              </section>
            )}
            {a.kind === "agent" && !a.archived && (
              <div className="flex justify-end border-t border-line p-3">
                <button className="btn" title={t("agent.new_key_title")} onClick={newKey}>
                  {t("agent.new_key")}
                </button>
              </div>
            )}
          </Panel>
          {a.name === "Access manager" && <CompanyAccessPanel />}
        </div>
      )}
      {tab === "instructions" && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <Panel title={t("agent.instructions")} className="min-w-0 lg:col-span-7" bodyClassName="max-h-[520px] overflow-y-auto">
            <pre className="px-4 py-3 font-mono text-xs whitespace-pre-wrap text-ink-2">{a.instructions ?? t("agent.builtin_instructions")}</pre>
            {a.kind !== "human" && <InstructionsEditor agentId={a.id} current={a.instructions} />}
          </Panel>
          <Panel title={t("agent.memory")} className="min-w-0 lg:col-span-5" bodyClassName="max-h-[520px] overflow-y-auto">
            {a.memory.length === 0 && <p className="px-4 py-3 text-sm text-ink-2">{t("agent.no_memory")}</p>}
            {a.memory.map((m) => (
              <p key={m.id} className="border-b border-line px-4 py-2 text-[13px] text-ink-2 last:border-0">
                {m.body}
              </p>
            ))}
          </Panel>
          <div className="flex min-w-0 flex-col gap-4 lg:col-span-12">
            <PlacePanel key={`${a.role}-${a.team}-${a.reports_to}`} a={a} onSaved={load} />
            {a.kind !== "human" && <EnginePanel key={`${a.engine}-${a.model}`} a={a} onSaved={load} />}
            <FeedbackPanel member={{ id: a.id, name: a.name }} />
            <SchedulesPanel actor={{ id: a.id, name: a.name }} />
          </div>
        </div>
      )}
    </div>
  );
}
