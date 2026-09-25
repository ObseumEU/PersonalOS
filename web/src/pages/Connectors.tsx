import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { PageHeader, Panel } from "../components/ui";
import { ago } from "./Agents";

type Rule = {
  id: number;
  name: string;
  source: string;
  match: Record<string, string>;
  assignee: string | null;
  priority: number | null;
  topic: string | null;
  enabled: boolean;
  hits: number;
};
type Event = {
  id: number;
  source: string;
  kind: string | null;
  title: string;
  received_at: string;
  rule_name: string | null;
  assignee_name: string | null;
  task_ref: string | null;
  task_status: string | null;
  signals: string;
  received_by_name: string | null;
};
type Status = { outbound: Record<string, boolean>; github_webhook: boolean; knowlage_ingest: string };

const SOURCES = ["gmail", "github", "discord", "calendar", "nexus", "web", "manual", "any"];
const input = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

const SETUP: Record<string, string> = {
  "email.send": "POS_SMTP_HOST, POS_SMTP_PORT, POS_SMTP_USER, POS_SMTP_PASSWORD, POS_SMTP_FROM",
  "github.comment": "POS_GITHUB_TOKEN",
  "discord.post": "POS_DISCORD_WEBHOOK_URL",
};

function matchText(m: Record<string, string>) {
  const parts = Object.entries(m).map(([k, v]) => `${k.replace("_", " ")} ${v}`);
  return parts.length ? parts.join(" · ") : "every event";
}

export default function Connectors() {
  const [status, setStatus] = useState<Status | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [rule, setRule] = useState({ name: "", source: "gmail", key: "text_regex", value: "", assignee: "", priority: "" });
  const [test, setTest] = useState({ source: "gmail", title: "", body: "", author: "" });

  const load = useCallback(() => {
    api<Status>("/api/connectors").then(setStatus);
    api<Rule[]>("/api/routes").then(setRules);
    api<Event[]>("/api/events").then(setEvents);
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const run = (p: Promise<unknown>) =>
    p.then(
      () => {
        setError(null);
        load();
      },
      (e) => setError(e.message),
    );

  function addRule(e: FormEvent) {
    e.preventDefault();
    run(
      api("/api/routes", {
        method: "POST",
        body: JSON.stringify({
          name: rule.name,
          source: rule.source,
          match: rule.value ? { [rule.key]: rule.value } : {},
          assignee: rule.assignee || null,
          priority: rule.priority ? Number(rule.priority) : null,
          position: 0,
        }),
      }),
    );
    setRule({ ...rule, name: "", value: "" });
  }

  function sendTest(e: FormEvent) {
    e.preventDefault();
    run(api("/api/events", { method: "POST", body: JSON.stringify({ ...test, labels: [] }) }));
    setTest({ ...test, title: "", body: "" });
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="SYSTEM · CONNECTORS · STEP 4"
        title="Connectors"
        sub="Incoming events (e-mail, GitHub, Discord) become tasks for the right member. Anything going out waits for your approval."
      />
      {error && <p className="cap text-red-400!">{error}</p>}
      <div className="grid gap-4 lg:grid-cols-12">
        <Panel fig="TAB. 14" title="What is set up" right="secrets stay on the server" className="lg:col-span-4">
          {status &&
            Object.entries(status.outbound).map(([k, on]) => (
              <div key={k} className="flex flex-col gap-1 border-b border-line px-4 py-2.5">
                <span className="flex items-center gap-2 text-[13px]">
                  <span className={`h-1.5 w-1.5 rounded-full ${on ? "bg-accent" : "bg-dim"}`} />
                  <span className="font-mono">{k}</span>
                  <span className={`cap ml-auto ${on ? "text-accent!" : ""}`}>{on ? "on" : "off · sent by hand"}</span>
                </span>
                {!on && <span className="cap">set {SETUP[k]}</span>}
              </div>
            ))}
          {status && (
            <>
              <div className="flex items-center gap-2 border-b border-line px-4 py-2.5 text-[13px]">
                <span className={`h-1.5 w-1.5 rounded-full ${status.github_webhook ? "bg-accent" : "bg-dim"}`} />
                GitHub webhook <span className="font-mono text-xs text-ink-3">/api/hooks/github</span>
                <span className="cap ml-auto">{status.github_webhook ? "on" : "off · POS_GITHUB_WEBHOOK_SECRET"}</span>
              </div>
              <p className="px-4 py-2.5 text-xs leading-relaxed text-ink-2">
                Knowledge: agents push what they read into knowlage-agent themselves, each with its own KB key ({status.knowlage_ingest}).
              </p>
            </>
          )}
        </Panel>

        <Panel fig="TAB. 15" title="Routing rules" right="first match wins · versioned · agents may propose changes" className="lg:col-span-8">
          <div className="grid grid-cols-[minmax(0,1.4fr)_70px_minmax(0,1fr)_130px_48px_44px_70px] gap-2 border-b border-line px-4 py-2">
            {["RULE", "SOURCE", "MATCH", "→ ASSIGNEE", "PRIO", "HITS", ""].map((h) => (
              <span key={h} className="cap">
                {h}
              </span>
            ))}
          </div>
          {rules.map((r) => (
            <div key={r.id} className={`grid grid-cols-[minmax(0,1.4fr)_70px_minmax(0,1fr)_130px_48px_44px_70px] items-center gap-2 border-b border-line px-4 py-2 text-[13px] ${r.enabled ? "" : "opacity-45"}`}>
              <span className="truncate">{r.name}</span>
              <span className="font-mono text-xs">{r.source}</span>
              <span className="cap truncate">{matchText(r.match)}</span>
              <span className="truncate text-accent">{r.assignee ?? "inbox"}</span>
              <span className="cap">{r.priority ? `P${r.priority}` : "—"}</span>
              <span className="font-mono text-xs">{r.hits}</span>
              <span className="flex gap-2">
                <button className="cap hover:text-accent!" onClick={() => run(api(`/api/routes/${r.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !r.enabled }) }))}>
                  {r.enabled ? "off" : "on"}
                </button>
                <button className="cap hover:text-red-400!" onClick={() => run(api(`/api/routes/${r.id}/archive`, { method: "POST" }))}>
                  archive
                </button>
              </span>
            </div>
          ))}
          <form onSubmit={addRule} className="flex flex-wrap items-center gap-2 px-4 py-3">
            <input required placeholder="New rule name" className={`${input} w-48`} value={rule.name} onChange={(e) => setRule({ ...rule, name: e.target.value })} />
            <select className={input} value={rule.source} onChange={(e) => setRule({ ...rule, source: e.target.value })}>
              {SOURCES.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
            <select className={input} value={rule.key} onChange={(e) => setRule({ ...rule, key: e.target.value })}>
              <option value="text_regex">text matches</option>
              <option value="from_contains">from contains</option>
              <option value="kind">kind is</option>
              <option value="label">has label</option>
            </select>
            <input placeholder="value" className={`${input} w-36`} value={rule.value} onChange={(e) => setRule({ ...rule, value: e.target.value })} />
            <input placeholder="assignee (me, ai, agent)" className={`${input} w-44`} value={rule.assignee} onChange={(e) => setRule({ ...rule, assignee: e.target.value })} />
            <select className={input} value={rule.priority} onChange={(e) => setRule({ ...rule, priority: e.target.value })}>
              <option value="">no priority</option>
              <option value="1">P1</option>
              <option value="2">P2</option>
              <option value="3">P3</option>
            </select>
            <button className="btn-accent">Add rule</button>
          </form>
        </Panel>
      </div>

      <div className="grid gap-4 lg:grid-cols-12">
        <Panel fig="LOG" title="Incoming events" right="content is stored as untrusted" className="lg:col-span-8">
          {events.length === 0 && <p className="cap p-4">No events yet. Connector agents report them with emit_event; GitHub through the webhook.</p>}
          {events.map((e) => (
            <div key={e.id} className="grid grid-cols-[70px_minmax(0,1fr)_minmax(0,0.9fr)_150px_70px] items-center gap-2 border-b border-line px-4 py-2 text-[13px]">
              <span className="font-mono text-xs">{e.source}</span>
              <span className="truncate">
                {e.title}
                {e.signals && <span className="cap ml-2 text-amber-300!">suspicious: {e.signals}</span>}
              </span>
              <span className="cap truncate">{e.rule_name ?? "no rule → inbox"}</span>
              <span className="truncate">
                {e.task_ref && (
                  <Link to={`/tasks?view=agents&task=${e.task_ref}`} className="text-accent hover:underline">
                    {e.task_ref}
                  </Link>
                )}{" "}
                <span className="text-ink-2">{e.assignee_name ?? "inbox"}</span>
              </span>
              <span className="cap text-right">{ago(e.received_at)}</span>
            </div>
          ))}
        </Panel>
        <Panel fig="TEST" title="Send a test event" right="see which rule catches it" className="lg:col-span-4">
          <form onSubmit={sendTest} className="flex flex-col gap-2 p-4">
            <select className={input} value={test.source} onChange={(e) => setTest({ ...test, source: e.target.value })}>
              {SOURCES.filter((s) => s !== "any").map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
            <input required placeholder="Title (e.g. subject)" className={input} value={test.title} onChange={(e) => setTest({ ...test, title: e.target.value })} />
            <input placeholder="From" className={input} value={test.author} onChange={(e) => setTest({ ...test, author: e.target.value })} />
            <textarea rows={3} placeholder="Body" className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent" value={test.body} onChange={(e) => setTest({ ...test, body: e.target.value })} />
            <button className="btn-accent self-start">Send event</button>
          </form>
        </Panel>
      </div>
    </div>
  );
}
