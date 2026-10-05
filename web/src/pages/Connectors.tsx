import { useCallback, useEffect, useState, type FormEvent } from "react";
import { TaskLink } from "../taskSheet";
import { api } from "../api";
import { confirmDialog, toast } from "../components/overlay";
import { PageHeader, Panel } from "../components/ui";
import { ago, label, t } from "../i18n";
import { envWords, ruleName } from "../settingsWords";

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
type Status = {
  outbound: Record<string, boolean>;
  github_webhook: boolean;
  knowlage_ingest: string;
  event_senders?: string[];
  mail_prefilter?: { days: number; total: number; by_reason: Record<string, number> };
};

// calendar, nexus and web come back when something sends them.
const SOURCES = ["gmail", "github", "discord", "manual", "any"];
const MATCH_KEYS = ["text_regex", "from_contains", "kind", "label"];
const input = "h-8 min-w-0 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

const SETUP: Record<string, string> = {
  "email.send": "POS_SMTP_HOST, POS_SMTP_PORT, POS_SMTP_USER, POS_SMTP_PASSWORD, POS_SMTP_FROM",
  "github.comment": "POS_GITHUB_TOKEN",
  "discord.post": "POS_DISCORD_WEBHOOK_URL",
  "github.issue": "POS_GITHUB_TOKEN",
  "github.review": "POS_GITHUB_TOKEN",
};
const NEVER_AUTO = new Set(["payment", "web.post"]);

/** The intake's suspicion signals in words ("duplicate of grafana-…" -> "duplikát"). */
const signalsCs = (s: string) =>
  s
    .split(/,\s*/)
    .map((x) => (x.startsWith("duplicate of") ? "duplikát" : x === "resolved" ? "vyřešené" : x.replace(/^support:triage$/, "zákaznický požadavek")))
    .join(", ");

// Name and match wrap instead of truncating to "GitHub issue l…"; the actions wrap too (1440 px fits).
// One column on a phone (each cell on its line, no sideways scrolling), the table from md up.
const RULE_GRID = "grid grid-cols-1 md:grid-cols-[minmax(0,1.6fr)_minmax(0,1.2fr)_minmax(0,0.9fr)_40px_48px_minmax(0,auto)] gap-x-3 gap-y-1";
const EVENT_GRID = "grid grid-cols-1 md:grid-cols-[80px_minmax(0,1fr)_minmax(0,0.9fr)_150px_90px] gap-x-2 gap-y-0.5";

function MatchOptions() {
  return (
    <>
      {MATCH_KEYS.map((k) => (
        <option key={k} value={k}>
          {t(`conn.match.${k}`)}
        </option>
      ))}
    </>
  );
}

function PriorityOptions() {
  return (
    <>
      <option value="">{t("conn.no_priority")}</option>
      <option value="1">P1</option>
      <option value="2">P2</option>
      <option value="3">P3</option>
    </>
  );
}

/** Every field of a routing rule, edited in place (versioned on the server). */
function EditRule({ r, onSave, onCancel }: { r: Rule; onSave: (changes: Record<string, unknown>) => void; onCancel: () => void }) {
  const firstKey = Object.keys(r.match)[0] ?? "text_regex";
  const [f, setF] = useState({
    name: r.name, source: r.source, key: firstKey, value: r.match[firstKey] ?? "", assignee: r.assignee ?? "",
    priority: r.priority ? String(r.priority) : "", topic: r.topic ?? "",
  });
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-line bg-raised px-4 py-2.5">
      <input className={`${input} w-44`} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} aria-label={t("conn.rule_name")} />
      <select className={input} value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })} aria-label={t("conn.source")}>
        {[...new Set([...SOURCES, r.source])].map((s) => (
          <option key={s} value={s}>
            {label("conn.src", s)}
          </option>
        ))}
      </select>
      <select className={input} value={f.key} onChange={(e) => setF({ ...f, key: e.target.value })} aria-label={t("conn.match")}>
        <MatchOptions />
      </select>
      <input className={`${input} w-36`} placeholder={t("conn.every_event_ph")} value={f.value} onChange={(e) => setF({ ...f, value: e.target.value })} aria-label={t("conn.match_value")} />
      <input className={`${input} w-36`} placeholder={t("conn.assignee")} value={f.assignee} onChange={(e) => setF({ ...f, assignee: e.target.value })} aria-label={t("conn.assignee")} />
      <select className={input} value={f.priority} onChange={(e) => setF({ ...f, priority: e.target.value })} aria-label={t("priority.label")}>
        <PriorityOptions />
      </select>
      <input className={`${input} w-28`} placeholder={t("conn.topic")} value={f.topic} onChange={(e) => setF({ ...f, topic: e.target.value })} aria-label={t("conn.topic")} />
      <button
        type="button"
        className="btn-accent"
        onClick={() =>
          onSave({
            name: f.name, source: f.source, match: f.value ? { [f.key]: f.value } : {}, assignee: f.assignee || null,
            priority: f.priority ? Number(f.priority) : null, topic: f.topic || null,
          })
        }
      >
        {t("act.save")}
      </button>
      <button type="button" className="btn" onClick={onCancel}>
        {t("act.cancel")}
      </button>
    </div>
  );
}

function matchText(m: Record<string, string>) {
  const parts = Object.entries(m).map(([k, v]) => `${label("conn.match", k)} ${v}`);
  return parts.length ? parts.join(" · ") : t("conn.every_event");
}

export default function Connectors() {
  const [status, setStatus] = useState<Status | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [rule, setRule] = useState({ name: "", source: "gmail", key: "text_regex", value: "", assignee: "", priority: "" });
  const [editing, setEditing] = useState<number | null>(null);
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
        return true;
      },
      (e) => {
        setError(e.message);
        return false;
      },
    );

  const setEnabled = (r: Rule, enabled: boolean) =>
    run(api(`/api/routes/${r.id}`, { method: "PATCH", body: JSON.stringify({ enabled }) }));

  function toggle(r: Rule) {
    setEnabled(r, !r.enabled).then((ok) => {
      if (ok) toast(t(r.enabled ? "conn.paused_toast" : "conn.resumed_toast", { name: r.name }), { undo: () => setEnabled(r, r.enabled) });
    });
  }

  async function archive(r: Rule) {
    const ok = await confirmDialog({ title: t("conn.archive_title", { name: r.name }), body: t("conn.archive_body"), confirm: t("act.archive"), danger: true });
    if (ok === null) return;
    run(api(`/api/routes/${r.id}/archive`, { method: "POST" })).then((done) => done && toast(t("conn.archived_toast", { name: r.name })));
  }

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
    <div className="flex min-w-0 flex-col gap-5">
      <PageHeader kicker={t("settings.kicker")} title={t("nav.connectors")} sub={t("conn.sub")} />
      {error && <p className="text-xs break-words text-red-400">{error}</p>}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel title={t("conn.setup")} right={t("conn.setup_right")} className="min-w-0 lg:col-span-4">
          {status &&
            Object.entries(status.outbound).map(([k, on]) => (
              <div key={k} className="flex min-w-0 flex-col gap-1 border-b border-line px-4 py-2.5">
                <span className="flex min-w-0 items-center gap-2 text-[13px]">
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${on ? "bg-accent" : "bg-dim"}`} />
                  <span className="min-w-0 truncate" title={k}>
                    {label("conn.out", k)}
                  </span>
                  <span className={`ml-auto shrink-0 text-xs ${on ? "text-accent" : "text-ink-2"}`}>{on ? t("conn.on") : t("conn.off_by_hand")}</span>
                </span>
                {!on && (
                  <span className="text-xs break-words text-ink-2" title={SETUP[k]}>
                    {NEVER_AUTO.has(k) ? t("conn.never_auto") : SETUP[k] ? t("conn.set_env", { env: envWords(SETUP[k]) }) : t(k === "linkedin.post" ? "conn.linkedin_connect" : "conn.set_server")}
                  </span>
                )}
              </div>
            ))}
          {status && (
            <>
              <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5 text-[13px]">
                <span className={`h-1.5 w-1.5 rounded-full ${status.github_webhook ? "bg-accent" : "bg-dim"}`} />
                {t("conn.gh_webhook")} <span className="font-mono text-xs text-ink-2">/api/hooks/github</span>
                <span className="ml-auto text-xs text-ink-2" title="POS_GITHUB_WEBHOOK_SECRET">
                  {status.github_webhook ? t("conn.on") : t("conn.off_env", { env: envWords("POS_GITHUB_WEBHOOK_SECRET") })}
                </span>
              </div>
              <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5 text-[13px]">
                <span className={`h-1.5 w-1.5 rounded-full ${status.event_senders?.length ? "bg-accent" : "bg-dim"}`} />
                {t("conn.machine_events")} <span className="font-mono text-xs text-ink-2">/api/events</span>
                <span className="ml-auto text-xs text-ink-2" title="POS_EVENTS_TOKENS">
                  {status.event_senders?.length ? status.event_senders.join(", ") : t("conn.off_env", { env: envWords("POS_EVENTS_TOKENS") })}
                </span>
              </div>
              {status.mail_prefilter && (
                <div className="flex flex-col gap-1 border-b border-line px-4 py-2.5">
                  <span className="flex items-center gap-2 text-[13px]">
                    {t("conn.prefilter")}
                    <span className="ml-auto text-xs text-ink-2">
                      {t("conn.prefilter_stats", { n: status.mail_prefilter.total, days: status.mail_prefilter.days })}
                    </span>
                  </span>
                  {Object.entries(status.mail_prefilter.by_reason).map(([reason, n]) => (
                    <span key={reason} className="flex justify-between gap-2 text-xs text-ink-2">
                      <span className="truncate">{reason}</span>
                      <span className="font-mono">{n}</span>
                    </span>
                  ))}
                  {status.mail_prefilter.total === 0 && <span className="text-xs text-ink-2">{t("conn.prefilter_empty")}</span>}
                </div>
              )}
              <p className="px-4 py-2.5 text-xs leading-relaxed break-words text-ink-2">{t("conn.knowledge", { ingest: status.knowlage_ingest })}</p>
            </>
          )}
        </Panel>

        <Panel title={t("conn.rules")} right={t("conn.rules_right")} className="min-w-0 lg:col-span-8">
          <div>
            <div>
              <div className={`${RULE_GRID.replace(/^grid /, "")} hidden border-b border-line px-4 py-2 text-xs text-ink-2 md:grid`}>
                <span>{t("conn.h.rule")}</span>
                <span>
                  {t("conn.source")} · {t("conn.match").toLowerCase()}
                </span>
                <span>{t("conn.h.assignee")}</span>
                <span>{t("conn.h.prio")}</span>
                <span>{t("conn.h.hits")}</span>
                <span />
              </div>
              {rules.map((r) =>
                editing === r.id ? (
                  <EditRule
                    key={r.id}
                    r={r}
                    onCancel={() => setEditing(null)}
                    onSave={(changes) =>
                      run(api(`/api/routes/${r.id}`, { method: "PATCH", body: JSON.stringify(changes) })).then((ok) => {
                        if (ok) {
                          setEditing(null);
                          toast(t("act.saved"));
                        }
                      })
                    }
                  />
                ) : (
                  <div key={r.id} className={`${RULE_GRID} items-center border-b border-line px-4 py-2 text-[13px]`}>
                    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                      <span className="min-w-0 break-words" title={r.name}>
                        {ruleName(r.name)}
                      </span>
                      {!r.enabled && <span className="shrink-0 rounded border border-amber-400/60 px-1.5 text-xs text-amber-300">{t("status.paused_badge")}</span>}
                    </span>
                    <span className="flex min-w-0 flex-col text-xs">
                      <span>{label("conn.src", r.source)}</span>
                      <span className="truncate text-ink-2" title={matchText(r.match)}>
                        {matchText(r.match)}
                      </span>
                    </span>
                    <span className="truncate text-accent">{r.assignee ?? t("conn.inbox")}</span>
                    <span className="text-xs text-ink-2">{r.priority ? `P${r.priority}` : "—"}</span>
                    <span className="font-mono text-xs">{r.hits}</span>
                    <span className="flex flex-wrap gap-x-3 gap-y-1 text-xs md:justify-end">
                      <button className="text-ink-2 hover:text-accent" onClick={() => toggle(r)}>
                        {r.enabled ? t("act.pause") : t("act.resume")}
                      </button>
                      <button className="text-ink-2 hover:text-accent" onClick={() => setEditing(r.id)}>
                        {t("act.edit")}
                      </button>
                      <button className="text-ink-2 hover:text-red-400" onClick={() => archive(r)}>
                        {t("act.archive")}
                      </button>
                    </span>
                  </div>
                ),
              )}
            </div>
          </div>
          <form onSubmit={addRule} className="flex flex-wrap items-center gap-2 px-4 py-3">
            <input required placeholder={t("conn.new_rule")} aria-label={t("conn.rule_name")} className={`${input} w-48`} value={rule.name} onChange={(e) => setRule({ ...rule, name: e.target.value })} />
            <select className={input} aria-label={t("conn.source")} value={rule.source} onChange={(e) => setRule({ ...rule, source: e.target.value })}>
              {SOURCES.map((s) => (
                <option key={s} value={s}>
                  {label("conn.src", s)}
                </option>
              ))}
            </select>
            <select className={input} aria-label={t("conn.match")} value={rule.key} onChange={(e) => setRule({ ...rule, key: e.target.value })}>
              <MatchOptions />
            </select>
            <input placeholder={t("conn.value")} aria-label={t("conn.match_value")} className={`${input} w-36`} value={rule.value} onChange={(e) => setRule({ ...rule, value: e.target.value })} />
            <input placeholder={t("conn.assignee_ph")} aria-label={t("conn.assignee")} className={`${input} w-44`} value={rule.assignee} onChange={(e) => setRule({ ...rule, assignee: e.target.value })} />
            <select className={input} aria-label={t("priority.label")} value={rule.priority} onChange={(e) => setRule({ ...rule, priority: e.target.value })}>
              <PriorityOptions />
            </select>
            <button className="btn-accent">{t("conn.add_rule")}</button>
          </form>
        </Panel>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel title={t("conn.events")} right={t("conn.events_right")} className="min-w-0 lg:col-span-8">
          {events.length === 0 && <p className="p-4 text-xs text-ink-2">{t("conn.events_empty")}</p>}
          {events.length > 0 && (
            <div>
              <div>
                {events.map((e) => (
                  <div key={e.id} className={`${EVENT_GRID} items-center border-b border-line px-4 py-2 text-[13px]`}>
                    <span className="text-xs">{label("conn.src", e.source)}</span>
                    <span className="truncate">
                      {e.title}
                      {e.signals && (
                        <span className="ml-2 text-xs text-amber-300" title={e.signals}>
                          {t("conn.suspicious", { signals: signalsCs(e.signals) })}
                        </span>
                      )}
                    </span>
                    <span className="truncate text-xs text-ink-2">{e.rule_name ? ruleName(e.rule_name) : t("conn.no_rule")}</span>
                    <span className="truncate">
                      {e.task_ref && (
                        <TaskLink taskRef={e.task_ref} className="text-accent hover:underline">
                          {e.task_ref}
                        </TaskLink>
                      )}{" "}
                      <span className="text-ink-2">{e.assignee_name ?? t("conn.inbox")}</span>
                    </span>
                    <span className="text-xs text-ink-2 md:text-right">{ago(e.received_at)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Panel>
        <Panel title={t("conn.test")} right={t("conn.test_right")} className="min-w-0 lg:col-span-4">
          <form onSubmit={sendTest} className="flex flex-col gap-2 p-4">
            <select className={input} aria-label={t("conn.source")} value={test.source} onChange={(e) => setTest({ ...test, source: e.target.value })}>
              {SOURCES.filter((s) => s !== "any").map((s) => (
                <option key={s} value={s}>
                  {label("conn.src", s)}
                </option>
              ))}
            </select>
            <input required placeholder={t("conn.test_title")} aria-label={t("conn.test_title")} className={input} value={test.title} onChange={(e) => setTest({ ...test, title: e.target.value })} />
            <input placeholder={t("conn.test_from")} aria-label={t("conn.test_from")} className={input} value={test.author} onChange={(e) => setTest({ ...test, author: e.target.value })} />
            <textarea
              rows={3}
              placeholder={t("conn.test_body")}
              aria-label={t("conn.test_body")}
              className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
              value={test.body}
              onChange={(e) => setTest({ ...test, body: e.target.value })}
            />
            <button className="btn-accent self-start">{t("conn.test_send")}</button>
          </form>
        </Panel>
      </div>
    </div>
  );
}
