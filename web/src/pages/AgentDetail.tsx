import { Pause, Play, RotateCcw, Send, Square, UserCheck } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { type AgentDetail as Detail, agentsApi, type Org } from "../agentsApi";
import { markdownSnippet } from "../markdownText";
import { AccessPanel, CompanyAccessPanel } from "../components/agents/AccessPanel";
import { AgentCredentialsPanel } from "./Credentials";
import { ActorChip, EngineBadge, Pill, StatusDot } from "../components/agents/bits";
import { AssigneeChip, StatePill } from "../components/tasks/bits";
import { FeedbackPanel, InstructionsEditor } from "../components/Feedback";
import { SchedulesPanel } from "../components/Schedules";
import { PageHeader, Panel } from "../components/ui";
import { tasksApi } from "../tasksApi";
import { ago, fmtTokens } from "./Agents";

type Message = {
  id: number;
  from_actor: number;
  to_actor: number;
  from_name: string;
  to_name: string;
  body: string;
  priority: "fyi" | "change_plan" | "stop";
  created_at: string;
  read_at: string | null;
  acked_at: string | null;
};
type Hr = { rating: { score: number | null; finished: number; quality: number | null; autonomy: number | null } | null; proposals: { kind: string; reason: string }[] };

const PRIORITY: Record<Message["priority"], string> = { fyi: "text-ink-2", change_plan: "text-accent", stop: "text-amber-300" };

function Inject({ agentId, current, onSent }: { agentId: number; current?: string; onSent: () => void }) {
  const [body, setBody] = useState("");
  const [priority, setPriority] = useState<Message["priority"]>("change_plan");
  const [error, setError] = useState<string | null>(null);
  const sendWithPriority = (e: FormEvent) => {
    e.preventDefault();
    if (!body.trim()) return;
    api(`/api/agents/${agentId}/message`, { method: "POST", body: JSON.stringify({ body, priority, task_id: current }) }).then(
      () => {
        setBody("");
        setError(null);
        onSent();
      },
      (err) => setError(err.message),
    );
  };
  return (
    <form onSubmit={sendWithPriority} className="flex flex-col gap-2 border-t border-line p-3">
      <span className="cap">INJECT INTO THE RUNNING SESSION · THE AGENT PICKS IT UP AT ITS NEXT STEP</span>
      <textarea
        rows={2}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="“The client moved the call to 14:00 — prepare for that instead.”"
        className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
      />
      <div className="flex items-center gap-2">
        {(["fyi", "change_plan", "stop"] as const).map((p) => (
          <label key={p} className={`cap flex items-center gap-1 ${PRIORITY[p]}!`}>
            <input type="radio" name="priority" className="accent-accent" checked={priority === p} onChange={() => setPriority(p)} />
            {p}
          </label>
        ))}
        <button className="btn-accent ml-auto">
          <Send size={13} /> Send
        </button>
      </div>
      {error && <span className="cap text-red-400!">{error}</span>}
    </form>
  );
}

/** Role, team and manager; only the owner may change them (the API enforces it). */
function OrgPanel({ a, onSaved }: { a: Detail; onSaved: (p: Promise<unknown>, done: string) => void }) {
  const [org, setOrg] = useState<Org | null>(null);
  useEffect(() => {
    agentsApi.org().then(setOrg);
  }, [a.id]);
  const managers = (org?.members ?? []).filter((m) => m.id !== a.id);
  const field = "h-7 rounded border border-line bg-bg px-2 font-mono text-xs outline-none focus:border-accent";
  return (
    <Panel fig="ORG" title="Place in the team" right="owner only · the lead comes from agents/<slug>/agent.json">
      <div className="flex flex-wrap items-end gap-4 p-4">
        <label className="flex flex-col gap-1">
          <span className="cap">ROLE</span>
          <input
            key={`role-${a.role}`}
            list="org-roles"
            aria-label="Role"
            defaultValue={a.role ?? ""}
            onBlur={(e) => (e.target.value || null) !== a.role && onSaved(agentsApi.setOrg(a.id, { role: e.target.value || null }), "Role saved")}
            className={`${field} w-44`}
          />
          <datalist id="org-roles">
            {org?.roles.map((r) => <option key={r} value={r} />)}
          </datalist>
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">TEAM</span>
          <input
            key={`team-${a.team}`}
            aria-label="Team"
            defaultValue={a.team ?? ""}
            onBlur={(e) => (e.target.value || null) !== a.team && onSaved(agentsApi.setOrg(a.id, { team: e.target.value || null }), "Team saved")}
            className={`${field} w-40`}
          />
        </label>
        {!a.is_owner && (
          <label className="flex flex-col gap-1">
            <span className="cap">REPORTS TO</span>
            <select
              aria-label="Reports to"
              value={a.reports_to ?? ""}
              onChange={(e) => onSaved(agentsApi.setOrg(a.id, { reports_to: e.target.value ? Number(e.target.value) : null }), "Manager saved")}
              className={`${field} w-48`}
            >
              <option value="">default (COO)</option>
              {managers.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.is_owner ? "You (owner)" : m.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <Link to="/team?tab=structure" className="btn ml-auto">
          Org →
        </Link>
      </div>
    </Panel>
  );
}

export default function AgentDetail() {
  const id = Number(useParams().id);
  const [a, setA] = useState<(Detail & { hr?: Hr }) | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [perms, setPerms] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => {
    agentsApi.get(id).then(setA, (e) => setError(e.message));
    api<Message[]>(`/api/agents/${id}/messages`).then(setMessages);
  }, [id]);
  useEffect(() => {
    load();
    agentsApi.list().then((d) => setPerms(d.permissions));
    const t = setInterval(load, 5000); // live run view
    return () => clearInterval(t);
  }, [load]);

  async function act(p: Promise<unknown>, done?: string) {
    try {
      const out = (await p) as { api_key?: string };
      setNotice(out?.api_key ? `New API key (shown once): ${out.api_key}` : done ?? null);
      setError(null);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!a) return <p className="cap p-6">{error ?? "loading…"}</p>;
  const running = a.runs.find((r) => r.status === "running");
  const current = a.queue.find((t) => t.status === "working");

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker={`AGENTS · ${a.name.toUpperCase()} · ${a.runtime.toUpperCase()}`}
        title={a.name}
        sub={a.purpose ?? "Built-in member"}
      />
      <div className="flex flex-wrap items-center gap-2">
        <ActorChip a={a} />
        <StatusDot status={a.status} />
        {a.system && <Pill>system</Pill>}
        {a.lifetime && <Pill>{a.lifetime.replace("_", "-")}</Pill>}
        {a.role && <Pill>{a.role.replace(/_/g, " ")}</Pill>}
        {a.team && <span className="cap">team {a.team}</span>}
        {a.reports_to_name && (
          <Link to={`/team/${a.reports_to}`} className="cap hover:text-accent!">
            reports to {a.reports_to_name}
          </Link>
        )}
        <EngineBadge view={a.engine_view} engine={a.engine_effective} model={a.engine_effective !== "codex" ? a.model ?? "claude-opus-5-5" : null} />
        {!a.is_owner && (
          <select
            aria-label="Runtime"
            className="h-7 rounded border border-line bg-bg px-1.5 text-xs outline-none focus:border-accent"
            value={a.engine ?? ""}
            onChange={(e) => act(agentsApi.setEngine(id, e.target.value || null, a.model), "Runtime saved")}
          >
            <option value="">default runtime</option>
            <option value="claude">Claude CLI</option>
            <option value="codex">Codex CLI</option>
            <option value="auto">auto (Codex, then Claude)</option>
          </select>
        )}
        {!a.is_owner && (
          <input
            key={a.model ?? "default-model"}
            aria-label="Claude model"
            placeholder="claude-opus-5-5"
            defaultValue={a.model ?? ""}
            onBlur={(e) => (e.target.value || null) !== a.model && act(agentsApi.setEngine(id, a.engine, e.target.value || null), "Model saved")}
            className="h-7 w-40 rounded border border-line bg-bg px-2 font-mono text-xs outline-none focus:border-accent"
          />
        )}
        <span className="cap">
          created by {a.created_by_name ?? "platform"} · heartbeat {ago(a.last_seen_at)}
        </span>
        {a.kind !== "human" && (<span className="ml-auto flex flex-wrap gap-2">
          {a.paused ? (
            <button className="btn" onClick={() => act(agentsApi.action(id, "resume"))}>
              <Play size={13} /> Resume
            </button>
          ) : (
            <button className="btn" onClick={() => act(agentsApi.action(id, "pause"))}>
              <Pause size={13} /> Pause
            </button>
          )}
          {current && (
            <button className="btn" title="Take the current task over yourself" onClick={() => act(tasksApi.update(current.ref, { assignee: "me" }), `${current.ref} is yours now`)}>
              <UserCheck size={13} /> Take over
            </button>
          )}
          <button className="btn border-amber-400/60! text-amber-300!" onClick={() => act(agentsApi.action(id, "stop"), "Stopped and paused")}>
            <Square size={13} /> Stop
          </button>
          {!a.archived && a.kind === "agent" && (
            <button
              className="btn"
              title="Issue a new API key for this agent's worker (the old one stops working)"
              onClick={() => window.confirm("Issue a new key? The current one stops working.") && act(api(`/api/agents/${id}/key`, { method: "POST" }))}
            >
              New key
            </button>
          )}
          {a.archived ? (
            <button className="btn" onClick={() => act(agentsApi.action(id, "restore"))}>
              Restore
            </button>
          ) : (
            !a.system && (
              <button className="btn" onClick={() => window.confirm(`Archive ${a.name}? It can be restored.`) && act(agentsApi.action(id, "archive"), "Archived")}>
                Archive
              </button>
            )
          )}
        </span>)}
      </div>
      {notice && <p className="cap break-all text-accent!">{notice}</p>}
      {error && <p className="cap text-red-400!">{error}</p>}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel fig="QUEUE" title={`Assigned to ${a.name}`} right={`${a.queued} next · ${a.review} review`} className="lg:col-span-3" bodyClassName="overflow-y-auto max-h-[560px]">
          {a.queue.length === 0 && <p className="cap p-4">No tasks.</p>}
          {a.queue.map((t) => (
            <Link key={t.id} to={`/tasks?view=agents&task=${t.ref}`} className={`flex flex-col gap-1 border-b border-line px-4 py-2.5 hover:bg-raised ${t.status === "working" ? "bg-raised shadow-[inset_2px_0_0_var(--color-accent)]" : ""}`}>
              <span className="flex items-center gap-2">
                <span className="cap">{t.ref}</span>
                <StatePill task={t} />
              </span>
              <span className="text-[13px]">{t.title}</span>
              {t.progress_note && <span className="cap truncate">{markdownSnippet(t.progress_note, 160)}</span>}
            </Link>
          ))}
        </Panel>

        <Panel
          fig={running ? `RUN ${running.id}` : "RUN"}
          title={current ? `${current.ref} · ${current.title}` : "Live run"}
          right={running ? `codex exec · started ${ago(running.started_at)}` : "not running"}
          className="lg:col-span-6"
          bodyClassName="flex flex-col"
        >
          <div className="flex flex-wrap gap-6 border-b border-line px-4 py-3">
            <span className="flex flex-col gap-1">
              <span className="cap">STATUS</span>
              <span className="text-[13px] text-accent">{running ? `working · ${current?.progress ?? 0} %` : a.status}</span>
            </span>
            <span className="flex flex-col gap-1">
              <span className="cap">DONE WHEN</span>
              <span className="text-[13px]">{current?.definition_of_done ?? "—"}</span>
            </span>
            {(a.pending_gates ?? []).length > 0 && (
              <span className="flex flex-col gap-1">
                <span className="cap">HUMAN GATE</span>
                {a.pending_gates!.map((g) => (
                  <Link key={`${g.kind}-${g.id}`} to={g.link} className="text-[13px] text-amber-300! hover:underline">
                    čeká na tvé {g.kind === "approval" ? "schválení" : "rozhodnutí"}: {g.title} →
                  </Link>
                ))}
              </span>
            )}
          </div>
          <span className="cap px-4 pt-3 pb-1">TRACE · WHAT THE AGENT DID</span>
          <div className="max-h-[300px] overflow-y-auto">
            {a.trace.length === 0 && <p className="cap px-4 py-2">No activity yet.</p>}
            {a.trace.map((e) => (
              <div key={e.id} className="grid grid-cols-[62px_150px_46px_minmax(0,1fr)] gap-2.5 border-b border-line px-4 py-1.5 font-mono text-[11px]">
                <span className="text-ink-3">{new Date(e.at).toLocaleTimeString("en-GB")}</span>
                <span className="truncate text-accent">{e.action.replace(/^mcp:/, "")}</span>
                <span className="text-ink-3">{e.via}</span>
                <span className="truncate text-ink-2">
                  {typeof e.detail.screenshot === "string" && (
                    // Browser / computer use: the screenshot of this step (pos.browser, the run's artifacts).
                    <a href={`/api/browser/screenshots/${e.detail.screenshot}`} target="_blank" rel="noreferrer" className="mr-1.5 text-accent! hover:underline">
                      [screenshot]
                    </a>
                  )}
                  {e.entity ? `${e.entity} ${e.entity_id ?? ""}` : ""} {Object.keys(e.detail).length ? JSON.stringify(e.detail) : ""}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-auto">
            <Inject agentId={id} current={current?.ref} onSent={load} />
          </div>
        </Panel>

        <div className="flex flex-col gap-4 lg:col-span-3">
          <Panel
            fig="INBOX"
            title="Messages"
            right={
              <span className="flex items-center gap-2">
                {messages.filter((m) => m.to_actor === id && !m.read_at).length} unread
                <Link to={`/chat?dm=${id}`} className="text-accent!">open DM in chat →</Link>
              </span>
            }
            bodyClassName="max-h-[240px] overflow-y-auto"
          >
            {messages.length === 0 && <p className="cap p-4">No messages.</p>}
            {messages.map((m) => (
              <div key={m.id} className="flex flex-col gap-0.5 border-b border-line px-4 py-2">
                <span className="cap">
                  {m.from_name} → {m.to_name} · <span className={`${PRIORITY[m.priority]}!`}>{m.priority}</span> · {ago(m.created_at)}
                  {m.acked_at ? " · acted on" : m.read_at ? " · read" : " · unread"}
                </span>
                <span className="text-[13px]">{m.body}</span>
              </div>
            ))}
          </Panel>
          <Panel fig="BUDGET" title="Tokens" right={a.budget_class ?? "normal"} bodyClassName="grid grid-cols-2">
            {[
              ["24 H", fmtTokens(a.tokens_24h)],
              ["7 DAYS", fmtTokens(a.tokens_7d)],
              ["DAILY CAP", a.daily_cap ? fmtTokens(a.daily_cap) : "none"],
              ["RUNS", String(a.runs.length)],
            ].map(([k, v]) => (
              <div key={k} className="flex flex-col border-r border-b border-line px-4 py-2 [&:nth-child(2n)]:border-r-0">
                <span className="cap">{k}</span>
                <span className="font-mono text-lg">{v}</span>
              </div>
            ))}
          </Panel>
          <Panel fig="HR" title="Performance" right="this week">
            <div className="grid grid-cols-3 border-b border-line">
              {[
                ["DONE", a.week.done],
                ["RETURNED", a.week.returned],
                ["YOU STEPPED IN", a.week.interventions],
              ].map(([k, v]) => (
                <div key={k} className="flex flex-col px-4 py-2">
                  <span className="cap text-[9px]!">{k}</span>
                  <span className="font-mono text-lg">{v}</span>
                </div>
              ))}
            </div>
            <p className="px-4 py-2 text-xs text-ink-2">
              HR score:{" "}
              {a.hr?.rating?.score == null ? "not enough finished work yet" : `${Math.round(a.hr.rating.score * 100)} / 100`}
            </p>
            {a.hr?.proposals.map((p, i) => (
              <p key={i} className="px-4 pb-2 text-xs text-ink-2">
                <span className="font-mono text-accent">{p.kind}</span> {p.reason}
              </p>
            ))}
          </Panel>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel fig="TAB. 11" title="Recent runs" right="roll back everything a run changed" className="lg:col-span-6">
          {a.runs.length === 0 && <p className="cap p-4">No runs yet.</p>}
          {a.runs.map((r) => (
            <div key={r.id} className="grid grid-cols-[52px_80px_minmax(0,1fr)_90px_28px] items-center gap-2.5 border-b border-line px-4 py-2 text-xs">
              <span className="cap">#{r.id}</span>
              <span className={r.status === "ok" ? "text-accent" : r.status === "running" ? "text-accent" : "text-ink-3"}>{r.status}</span>
              <span className="truncate text-ink-2">
                {r.kind} · {ago(r.started_at)}
                {r.tool_calls != null ? ` · ${r.tool_calls} tool calls` : ""}
                {r.turns != null ? ` · ${r.turns} turns` : ""} {r.detail ? `· ${r.detail.slice(0, 80)}` : ""}
              </span>
              <span className="cap text-right">{r.input_tokens != null ? fmtTokens((r.input_tokens ?? 0) + (r.output_tokens ?? 0)) : "—"} tok</span>
              <button
                title={`Undo everything run ${r.id} changed`}
                className="text-ink-3 hover:text-accent"
                onClick={() => window.confirm(`Roll back every change run ${r.id} made?`) && act(api(`/api/runs/${r.id}/rollback`, { method: "POST" }), `Run ${r.id} rolled back`)}
              >
                <RotateCcw size={13} />
              </button>
            </div>
          ))}
        </Panel>
        <Panel fig="TAB. 12" title="Permissions" right="owner · permanent grants (the Access manager uses Access below)" className="lg:col-span-3">
          <div className="flex flex-col gap-1.5 p-4">
            {Object.entries(perms).map(([p, why]) => (
              <label key={p} title={why} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  className="accent-accent"
                  disabled={a.is_owner}
                  checked={a.permissions.includes(p) || a.permissions.includes("*")}
                  onChange={(e) =>
                    act(agentsApi.setPermissions(id, e.target.checked ? [...a.permissions, p] : a.permissions.filter((x) => x !== p)), "Permissions saved")
                  }
                />
                <span className="font-mono">{p}</span>
              </label>
            ))}
          </div>
        </Panel>
        <Panel fig="MEMORY" title="Instructions and memory" className="lg:col-span-3" bodyClassName="max-h-[300px] overflow-y-auto">
          <pre className="px-4 py-3 font-mono text-[11px] whitespace-pre-wrap text-ink-2">{a.instructions ?? "Built-in instructions."}</pre>
          {a.kind !== "human" && <InstructionsEditor agentId={a.id} current={a.instructions} />}
          {a.memory.map((m) => (
            <p key={m.id} className="border-t border-line px-4 py-2 text-xs text-ink-2">
              {m.body}
            </p>
          ))}
        </Panel>
      </div>
      {a.kind !== "human" && <AccessPanel agentId={a.id} />}
      {a.kind !== "human" && <AgentCredentialsPanel agentId={a.id} />}
      {a.name === "Access manager" && <CompanyAccessPanel />}
      <OrgPanel a={a} onSaved={act} />
      <FeedbackPanel member={{ id: a.id, name: a.name }} />
      <SchedulesPanel actor={{ id: a.id, name: a.name }} />
      <p className="cap">
        Assignees: <AssigneeChip type="human" name="Owner" /> people · <AssigneeChip type="ai" name="AI" /> the assistant ·{" "}
        <AssigneeChip type="agent" name="Agent" /> agents
      </p>
    </div>
  );
}
