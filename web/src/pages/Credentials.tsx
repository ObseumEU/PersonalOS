import { Archive, Check, EyeOff, Loader2, Pencil, Plus, RefreshCw, ShieldCheck, Sparkles, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  type AgentPick,
  type CredGrant,
  type CredKind,
  type CredRequest,
  type Credential,
  type CredOverview,
  type Discovery,
  type RosterAgent,
  type SuggestedCred,
  type Suggestion,
  credentialsApi,
} from "../credentialsApi";
import Markdown from "../components/Markdown";
import {
  Advanced,
  AgentChip,
  AgentPicker,
  AuditList,
  Avatar,
  HoursPick,
  KIND_LABEL,
  KindIcon,
  czAgo,
  czLeft,
  errWord,
  field,
} from "../components/credentials/parts";
import { confirmDialog, toast } from "../components/overlay";
import { PageHeader, Panel } from "../components/ui";
import { t } from "../i18n";

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const list = (s: string) =>
  s
    .split(/[\n,]+/)
    .map((x) => x.trim())
    .filter(Boolean);
const tools = (xs: string[]) => xs.map((x) => x.replace("tool:", "")).join(", ");

// ------------------------------------------------------------------ act: run, toast (with undo), reload

type Act = (p: Promise<unknown>, ok?: string, undo?: () => Promise<unknown>) => Promise<boolean>;

function useAct(reload: () => void): Act {
  return useCallback(
    async (p, ok, undo) => {
      try {
        await p;
        if (ok) toast(ok, { undo: undo ? () => undo().then(reload, reload) : undefined });
        reload();
        return true;
      } catch (e) {
        toast(errText(e), { error: true });
        reload();
        return false;
      }
    },
    [reload],
  );
}

function Label({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`text-xs text-ink-2 ${className}`}>{children}</span>;
}

// ------------------------------------------------------------------ groups: one card per 1Password item

type Group = {
  key: string;
  title: string;
  kind: CredKind;
  creds: Credential[];
  primary: Credential;
  byAgent: Map<number, { name: string; grants: CredGrant[] }>;
  paused: CredGrant[];
  recommended: AgentPick[];
};

function groupsOf(v: CredOverview): Group[] {
  const map = new Map<string, Credential[]>();
  for (const c of v.credentials) {
    const k = c.item ?? c.name;
    map.set(k, [...(map.get(k) ?? []), c]);
  }
  return [...map.entries()].map(([key, creds]) => {
    const primary = creds.find((c) => !c.name.endsWith("-user")) ?? creds[0];
    const byAgent = new Map<number, { name: string; grants: CredGrant[] }>();
    for (const c of creds)
      for (const g of c.grants ?? []) {
        const e = byAgent.get(g.agent_id) ?? { name: g.agent_name, grants: [] };
        e.grants.push(g);
        byAgent.set(g.agent_id, e);
      }
    const names = new Set(creds.map((c) => c.name));
    const paused = v.paused.filter((g) => !g.active && names.has(g.credential) && !byAgent.has(g.agent_id));
    return { key, title: key, kind: primary.kind ?? "generic", creds, primary, byAgent, paused, recommended: primary.recommended ?? [] };
  });
}

// ------------------------------------------------------------------ requests

function RequestCard({ r, act, highlight }: { r: CredRequest; act: Act; highlight: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (highlight) ref.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [highlight]);
  const s = r.suggestion;
  const what = r.registered ? r.credential : s ? s.title : r.credential;
  const missing = !r.registered && !s;
  const deny = async () => {
    const why = await confirmDialog({ title: t("cred.deny_title", { agent: r.agent_name, what }), reason: t("cred.deny_reason"), confirm: t("act.reject"), danger: true });
    if (why === null) return;
    act(credentialsApi.decide(r.id, "deny", why), t("cred.denied_toast", { agent: r.agent_name }));
  };
  return (
    <div ref={ref} className={`panel flex min-w-0 flex-col gap-3 p-4 ${highlight ? "border-accent!" : "border-amber-400/50!"}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
        <Avatar name={r.agent_name} size={24} />
        <span className="min-w-0 break-words">
          <b className="font-medium">{r.agent_name}</b> {t("cred.wants")} <b className="font-medium text-accent">{what}</b>
          {r.scope ? <span className="text-ink-2"> ({t("cred.only", { scope: r.scope })})</span> : null}
          <span className="text-ink-2"> · {r.hours ? t("cred.for_hours", { n: r.hours }) : t("cred.forever")}</span>
        </span>
        <Label className="ml-auto shrink-0">{czAgo(r.created_at)}</Label>
      </div>
      <div className="min-w-0 text-sm break-words text-ink-2">
        <Label className="mr-1">{t("cred.because")}</Label>
        <Markdown text={r.why} compact className="inline" />
        {r.task_ref && <Label> · {t("cred.task", { ref: r.task_ref })}</Label>}
      </div>
      {!r.registered && s && (
        <p className="flex items-start gap-2 rounded border border-accent/40 bg-accent/5 px-3 py-2 text-xs text-ink-2">
          <Sparkles size={13} className="mt-0.5 shrink-0 text-accent" />
          <span className="min-w-0 break-words">
            {t("cred.not_registered", { kind: s.kind_label })}{" "}
            <span className="font-mono text-accent">{s.credentials.map((c) => c.name).join(" + ")}</span>
            {s.extra_grants.length ? t("cred.and_tool", { tools: tools(s.extra_grants) }) : ""}.
          </span>
        </p>
      )}
      {missing && <p className="rounded border border-amber-400/50 px-3 py-2 text-xs text-amber-300">{t("cred.missing")}</p>}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="btn-accent"
          disabled={missing}
          onClick={() => act(credentialsApi.decide(r.id, "grant", ""), t("cred.granted_toast", { agent: r.agent_name, what }))}
        >
          <Check size={14} /> {t("act.approve")}
        </button>
        <button type="button" className="btn" onClick={deny}>
          <X size={14} /> {t("act.reject")}
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ discovery

function DiscoveryCard({ s0, agents, act }: { s0: Suggestion; agents: RosterAgent[]; act: Act }) {
  const [s, setS] = useState(s0);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [creds, setCreds] = useState<SuggestedCred[]>(s0.credentials);
  const [pick, setPick] = useState<number[]>(s0.agents.map((a) => a.id));
  const [hours, setHours] = useState<number | null>(null);
  useEffect(() => {
    setS(s0);
    setCreds(s0.credentials);
    setPick(s0.agents.map((a) => a.id));
  }, [s0]);
  const chosen = pick.map((id) => agents.find((a) => a.id === id)?.name ?? `#${id}`);
  const apply = (cs: SuggestedCred[], ids: number[], h: number | null) => {
    setBusy(true);
    act(
      credentialsApi.register({ item_id: s.item_id, credentials: cs, agent_ids: ids, hours: h }),
      ids.length ? t("cred.registered_granted", { title: s.title, who: chosen.join(", ") }) : t("cred.registered", { title: s.title }),
    ).finally(() => setBusy(false));
  };
  const reKind = (k: CredKind) =>
    credentialsApi.suggest(s.item_id, k).then(
      (n) => {
        setS(n);
        setCreds(n.credentials);
        if (pick.length === 0) setPick(n.agents.map((a) => a.id));
      },
      () => undefined,
    );
  const setC = (i: number, patch: Partial<SuggestedCred>) => setCreds(creds.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const none = creds.length === 0;
  return (
    <div className="panel flex min-w-0 flex-col gap-3 p-4">
      <div className="flex min-w-0 items-start gap-2">
        <KindIcon kind={s.kind} size={17} />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate text-[15px] font-medium">{s.title}</span>
          <Label className="truncate">
            {s.kind_label}
            {s.known ? ` · ${s.known}` : ""}
            {s.source === "llm" ? t("cred.llm_suggestion") : !s.decided ? t("cred.unsure") : ""}
          </Label>
        </div>
        <button
          type="button"
          title={t("cred.hide_title")}
          aria-label={t("cred.hide")}
          className="shrink-0 text-ink-2 hover:text-ink"
          onClick={() => act(credentialsApi.dismiss(s.item_id, true), t("cred.hidden_toast", { title: s.title }), () => credentialsApi.dismiss(s.item_id, false))}
        >
          <EyeOff size={14} />
        </button>
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs">
        <dt className="pt-0.5 text-ink-2">{t("cred.usage")}</dt>
        <dd className="min-w-0 break-words text-ink">{s.usage || "—"}</dd>
        <dt className="pt-0.5 text-ink-2">{t("cred.where")}</dt>
        <dd className="min-w-0 font-mono break-words text-ink">
          {s.hosts.length ? s.hosts.join(", ") : <span className="font-sans text-ink-2">{t("cred.anywhere")}</span>}
        </dd>
        <dt className="pt-0.5 text-ink-2">{t("cred.to_whom")}</dt>
        <dd className="flex min-w-0 flex-wrap gap-1">
          {pick.length === 0 ? (
            <span className="text-ink-2">{t("cred.no_agent_found")}</span>
          ) : (
            pick.map((id) => {
              const a = agents.find((x) => x.id === id);
              return a ? <AgentChip key={id} name={a.name} /> : null;
            })
          )}
        </dd>
      </dl>
      {editing && (
        <div className="flex flex-col gap-3 rounded border border-line p-3 text-xs">
          <label className="flex flex-col gap-1">
            <Label>{t("cred.what_is_it")}</Label>
            <select value={s.kind} onChange={(e) => reKind(e.target.value as CredKind)} className={field}>
              {(Object.keys(KIND_LABEL) as CredKind[]).map((k) => (
                <option key={k} value={k}>
                  {KIND_LABEL[k]}
                </option>
              ))}
            </select>
          </label>
          {creds.map((c, i) => (
            <div key={i} className="flex flex-col gap-2 border-t border-line pt-2">
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <label className="flex min-w-0 flex-col gap-1">
                  <Label>{c.role === "user" ? t("cred.name_user") : t("cred.name")}</Label>
                  <input value={c.name} onChange={(e) => setC(i, { name: e.target.value.toLowerCase().replace(/[^a-z0-9_.-]/g, "-") })} className={field} />
                </label>
                <label className="flex min-w-0 flex-col gap-1">
                  <Label>{t("cred.op_field")}</Label>
                  <select
                    value={c.op_ref}
                    onChange={(e) => setC(i, { op_ref: e.target.value, field: s.fields.find((f) => f.op_ref === e.target.value)?.title ?? c.field })}
                    className={field}
                  >
                    {s.fields.map((f) => (
                      <option key={f.id} value={f.op_ref}>
                        {f.section ? `${f.section} / ` : ""}
                        {f.title} ({f.type})
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex min-w-0 flex-col gap-1 sm:col-span-2">
                  <Label>{t("cred.hosts_comma")}</Label>
                  <input value={c.allowed_hosts.join(", ")} onChange={(e) => setC(i, { allowed_hosts: list(e.target.value) })} className={field} />
                </label>
              </div>
              <Advanced>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <label className="flex min-w-0 flex-col gap-1">
                    <Label>{t("cred.env_var")}</Label>
                    <input value={c.env_var ?? ""} onChange={(e) => setC(i, { env_var: e.target.value.toUpperCase() || null })} className={field} />
                  </label>
                  <label className="flex min-w-0 flex-col gap-1">
                    <Label>{t("cred.header")}</Label>
                    <input value={c.header ?? ""} placeholder="Authorization: Bearer {value}" onChange={(e) => setC(i, { header: e.target.value || null })} className={field} />
                  </label>
                  <label className="flex min-w-0 flex-col gap-1">
                    <Label>{t("cred.commands_comma")}</Label>
                    <input value={c.allowed_commands.join(", ")} onChange={(e) => setC(i, { allowed_commands: list(e.target.value) })} className={field} />
                  </label>
                  <label className="flex min-w-0 flex-col gap-1">
                    <Label>{t("cred.tools_both")}</Label>
                    <input value={c.allowed_tools.join(", ")} onChange={(e) => setC(i, { allowed_tools: list(e.target.value) })} className={field} />
                  </label>
                  <span className="font-mono text-xs break-all text-ink-2 sm:col-span-2">{c.op_ref}</span>
                </div>
              </Advanced>
            </div>
          ))}
          <div className="flex flex-col gap-1 border-t border-line pt-2">
            <Label>{t("cred.grant_to")}</Label>
            <AgentPicker agents={agents} recommended={s.agents} value={pick} onChange={setPick} />
          </div>
          <label className="flex flex-wrap items-center gap-2">
            <Label>{t("cred.how_long")}</Label>
            <HoursPick value={hours} onChange={setHours} />
          </label>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn-accent" disabled={busy || none} onClick={() => apply(creds, pick, hours)}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
          {pick.length ? t("cred.register_grant") : t("cred.register")}
        </button>
        <button type="button" className="btn" onClick={() => setEditing(!editing)} aria-expanded={editing}>
          <Pencil size={13} /> {editing ? t("cred.done") : t("act.edit")}
        </button>
        {none && <Label>{t("cred.no_secret")}</Label>}
      </div>
    </div>
  );
}

function DiscoverySection({
  d,
  loading,
  agents,
  act,
  reload,
}: {
  d: Discovery | null;
  loading: boolean;
  agents: RosterAgent[];
  act: Act;
  reload: (refresh: boolean, hidden?: boolean) => void;
}) {
  const [showHidden, setShowHidden] = useState(false);
  if (!d && loading)
    return (
      <p className="flex items-center gap-2 text-xs text-ink-2">
        <Loader2 size={12} className="animate-spin" /> {t("cred.discovering")}
      </p>
    );
  if (!d || !d.enabled) return null;
  const items = d.items;
  return (
    <section className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-base font-medium">{t("cred.new_in_op")}</h2>
        <Label>{items.length ? t("cred.new_count", { n: items.length }) : ""}</Label>
        <span className="ml-auto flex items-center gap-3">
          {d.hidden.length > 0 && (
            <button
              type="button"
              className="text-xs text-ink-2 hover:text-accent"
              onClick={() => {
                setShowHidden(!showHidden);
                reload(false, !showHidden);
              }}
            >
              {showHidden ? t("cred.hide_hidden") : t("cred.hidden_count", { n: d.hidden.length })}
            </button>
          )}
          <button type="button" className="flex items-center gap-1 text-xs text-ink-2 hover:text-accent" onClick={() => reload(true, showHidden)} disabled={loading}>
            <RefreshCw size={11} className={loading ? "animate-spin" : ""} /> {t("cred.reload")}
          </button>
        </span>
      </div>
      {d.error && <p className="text-xs break-words text-red-400">{d.error}</p>}
      {!d.error && items.length === 0 && (
        <p className="panel px-4 py-3 text-sm text-ink-2">
          {t("cred.all_registered_a")} <b className="font-medium">{d.vault}</b> {t("cred.all_registered_b")}
        </p>
      )}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {items.map((s) =>
          s.hidden ? (
            <div key={s.item_id} className="panel flex min-w-0 items-center gap-2 px-4 py-2 text-sm text-ink-2">
              <EyeOff size={13} /> <span className="min-w-0 flex-1 truncate">{s.title}</span>
              <button
                type="button"
                className="text-xs text-ink-2 hover:text-accent"
                onClick={() => act(credentialsApi.dismiss(s.item_id, false), t("cred.visible_toast", { title: s.title }))}
              >
                {t("cred.show")}
              </button>
            </div>
          ) : (
            <DiscoveryCard key={s.item_id} s0={s} agents={agents} act={act} />
          ),
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ a registered credential (one 1Password item)

function GrantBox({ g, agents, act, onDone }: { g: Group; agents: RosterAgent[]; act: Act; onDone: () => void }) {
  const [pick, setPick] = useState<number[]>([]);
  const [hours, setHours] = useState<number | null>(null);
  const [scope, setScope] = useState("");
  const has = [...g.byAgent.keys()];
  const names = g.creds.map((c) => c.name);
  return (
    <div className="flex flex-col gap-3 rounded border border-accent/40 p-3 text-xs">
      <AgentPicker agents={agents} recommended={g.recommended} value={pick} onChange={setPick} exclude={has} />
      <div className="flex flex-wrap items-center gap-2">
        <HoursPick value={hours} onChange={setHours} />
        <select aria-label={t("cred.scope")} value={scope} onChange={(e) => setScope(e.target.value)} className={field}>
          <option value="">{t("cred.scope_all")}</option>
          <option value="command">{t("cred.scope_command")}</option>
          <option value="http">{t("cred.scope_http")}</option>
          {g.primary.allowed_hosts.map((h) => (
            <option key={h} value={h}>
              {t("cred.only", { scope: h })}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn-accent"
          disabled={pick.length === 0}
          onClick={() => {
            const who = pick.map((id) => agents.find((a) => a.id === id)?.name).join(", ");
            act(credentialsApi.grantMany(pick, names, hours, scope || null), t("cred.granted_many", { title: g.title, who })).then((ok) => ok && onDone());
          }}
        >
          <Plus size={14} /> {t("cred.grant")}
          {pick.length > 1 ? ` (${pick.length})` : ""}
        </button>
        <button type="button" className="btn" onClick={onDone}>
          {t("act.cancel")}
        </button>
      </div>
      {(g.primary.companions?.length ?? 0) > 0 && <Label>{t("cred.companion", { tools: tools(g.primary.companions!) })}</Label>}
    </div>
  );
}

function CredEdit({ c, act, onDone }: { c: Credential; act: Act; onDone: () => void }) {
  const [f, setF] = useState({
    env_var: c.env_var ?? "",
    header: c.header ?? "",
    hosts: c.allowed_hosts.join(", "),
    commands: c.allowed_commands.join(", "),
    tools: c.allowed_tools.join(", "),
    max: String(c.max_uses_hour),
    description: c.description,
  });
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const input = (k: keyof typeof f, name: string, extra: { placeholder?: string } = {}) => (
    <label className="flex min-w-0 flex-col gap-1">
      <Label>{name}</Label>
      <input value={f[k]} onChange={set(k)} className={field} {...extra} />
    </label>
  );
  return (
    <form
      className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2"
      onSubmit={(e) => {
        e.preventDefault();
        act(
          credentialsApi.update(c.id, {
            env_var: f.env_var.trim(),
            header: f.header.trim(),
            allowed_hosts: list(f.hosts),
            allowed_commands: list(f.commands),
            allowed_tools: list(f.tools),
            max_uses_hour: Number(f.max) || 60,
            description: f.description,
          }),
          t("cred.saved", { name: c.name }),
        ).then((ok) => ok && onDone());
      }}
    >
      {input("env_var", t("cred.env_var"))}
      {input("header", t("cred.header"), { placeholder: "Authorization: Bearer {value}" })}
      {input("hosts", t("cred.hosts"))}
      {input("commands", t("cred.commands"))}
      {input("tools", t("cred.tools"))}
      <label className="flex min-w-0 flex-col gap-1">
        <Label>{t("cred.max_hour")}</Label>
        <input value={f.max} onChange={(e) => setF({ ...f, max: e.target.value.replace(/[^0-9]/g, "") })} className={field} />
      </label>
      <label className="flex min-w-0 flex-col gap-1 sm:col-span-2">
        <Label>{t("cred.description")}</Label>
        <textarea value={f.description} onChange={set("description")} rows={2} className={`${field} h-auto! py-1`} />
      </label>
      <div className="flex gap-2 sm:col-span-2">
        <button className="btn-accent h-7!">
          <Check size={13} /> {t("act.save")}
        </button>
        <button type="button" className="btn h-7!" onClick={onDone}>
          {t("act.cancel")}
        </button>
      </div>
    </form>
  );
}

function StatusLine({ g }: { g: Group }) {
  const last = g.creds
    .map((c) => c.last_use)
    .filter(Boolean)
    .sort((a, b) => (a!.at < b!.at ? 1 : -1))[0];
  const tests = g.creds.map((c) => c.last_test).filter(Boolean);
  const failed = tests.find((x) => !x!.ok);
  const test = failed ?? tests.sort((a, b) => (a!.at < b!.at ? 1 : -1))[0];
  const errors = g.creds.reduce((n, c) => n + (c.errors_24h ?? 0), 0);
  const uses = g.creds.reduce((n, c) => n + (c.uses_24h ?? 0), 0);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-2">
      <span>
        {last ? (
          <>
            {t("cred.last_used", { when: czAgo(last.at) })}
            {last.agent ? ` · ${last.agent}` : ""} · <span className={last.ok ? "" : "text-red-400"}>{last.ok ? t("cred.ok") : t("cred.denied")}</span>
          </>
        ) : (
          <span>{t("cred.unused")}</span>
        )}
      </span>
      {uses > 0 && <span>{t("cred.uses_24h", { n: uses })}</span>}
      {errors > 0 && <span className="rounded border border-red-400/50 px-1.5 text-xs text-red-400">{t("cred.errors_24h", { errors: errWord(errors) })}</span>}
      {test && (
        <span className={`break-words ${test.ok ? "text-accent" : "text-red-400"}`} title={test.error ?? undefined}>
          {test.ok ? t("cred.test_ok", { when: czAgo(test.at) }) : t("cred.test_failed", { when: czAgo(test.at), error: test.error ?? "" })}
        </span>
      )}
    </div>
  );
}

function CredentialCard({ g, agents, act }: { g: Group; agents: RosterAgent[]; act: Act }) {
  const [granting, setGranting] = useState(false);
  const [editing, setEditing] = useState<number | null>(null);
  const [testing, setTesting] = useState(false);
  const removeAgent = (name: string, grants: CredGrant[]) => {
    const ids = grants.map((x) => x.id);
    act(credentialsApi.revokeMany(ids), t("cred.revoked", { name, title: g.title }), () => credentialsApi.restoreMany(ids));
  };
  const test = () => {
    setTesting(true);
    act(
      Promise.all(g.creds.map((c) => credentialsApi.test(c.id).then((r) => ({ c, r })))).then((rs) => {
        const bad = rs.filter((x) => !x.r.ok);
        if (bad.length) throw new Error(t("cred.test_failed_list", { list: bad.map((x) => `${x.c.name}: ${x.r.error}`).join("; ") }));
      }),
      t("cred.test_ok_toast", { title: g.title }),
    ).finally(() => setTesting(false));
  };
  const archive = async () => {
    const ok = await confirmDialog({ title: t("cred.archive_title", { title: g.title }), body: t("cred.archive_body"), confirm: t("act.archive"), danger: true });
    if (ok === null) return;
    act(Promise.all(g.creds.map((c) => credentialsApi.archive(c.id, t("cred.archive_reason")))), t("cred.archived", { title: g.title }));
  };
  return (
    <div className="panel flex min-w-0 flex-col gap-3 p-4">
      <div className="flex min-w-0 items-start gap-2">
        <KindIcon kind={g.kind} size={17} />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate text-[15px] font-medium">{g.title}</span>
          <Label className="truncate">
            {KIND_LABEL[g.kind]} · <span className="font-mono">{g.creds.map((c) => c.name).join(" + ")}</span>
          </Label>
        </div>
      </div>
      {g.primary.description && <Markdown text={g.primary.description} compact className="text-xs text-ink-2" />}
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        {g.byAgent.size === 0 && g.paused.length === 0 && <Label>{t("cred.nobody_has")}</Label>}
        {[...g.byAgent.entries()].map(([id, e]) => (
          <AgentChip key={id} name={e.name} grant={e.grants.find((x) => x.credential === g.primary.name) ?? e.grants[0]} onRemove={() => removeAgent(e.name, e.grants)} />
        ))}
        {g.paused.map((p) => (
          <AgentChip key={p.id} name={p.agent_name} paused onResume={() => act(credentialsApi.resume(p.id), t("cred.resumed", { name: p.agent_name }))} />
        ))}
      </div>
      <StatusLine g={g} />
      {granting && <GrantBox g={g} agents={agents} act={act} onDone={() => setGranting(false)} />}
      <div className="flex flex-wrap gap-2">
        {!granting && (
          <button type="button" className="btn-accent h-8!" onClick={() => setGranting(true)}>
            <Plus size={14} /> {t("cred.grant")}
          </button>
        )}
        <button type="button" className="btn h-8!" disabled={testing} onClick={test}>
          {testing ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />} {t("cred.test")}
        </button>
        <button type="button" className="btn h-8!" onClick={archive}>
          <Archive size={13} /> {t("act.archive")}
        </button>
      </div>
      <Advanced>
        <div className="flex flex-col gap-3">
          {g.creds.map((c) => (
            <div key={c.id} className="flex min-w-0 flex-col gap-1 border-t border-line pt-2 text-xs">
              <div className="flex min-w-0 items-center gap-2">
                <span className="min-w-0 truncate font-mono text-accent">{c.name}</span>
                <button type="button" className="ml-auto text-xs text-ink-2 hover:text-accent" onClick={() => setEditing(editing === c.id ? null : c.id)}>
                  {editing === c.id ? t("act.close") : t("act.edit")}
                </button>
              </div>
              {editing === c.id ? (
                <CredEdit c={c} act={act} onDone={() => setEditing(null)} />
              ) : (
                <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5">
                  <dt className="text-ink-2">1Password</dt>
                  <dd className="font-mono break-all text-ink">{c.op_ref}</dd>
                  <dt className="text-ink-2">{t("cred.dt.env")}</dt>
                  <dd className="font-mono break-all text-ink">{c.env_var ?? `CRED_${c.name.toUpperCase().replace(/[^A-Z0-9]/g, "_")}`}</dd>
                  <dt className="text-ink-2">{t("cred.dt.header")}</dt>
                  <dd className="font-mono break-all text-ink">{c.header ?? "—"}</dd>
                  <dt className="text-ink-2">{t("cred.dt.hosts")}</dt>
                  <dd className="font-mono break-all text-ink">{c.allowed_hosts.join(", ") || "—"}</dd>
                  <dt className="text-ink-2">{t("cred.dt.commands")}</dt>
                  <dd className="font-mono break-all text-ink">{c.allowed_commands.join(", ") || "—"}</dd>
                  <dt className="text-ink-2">{t("cred.dt.tools")}</dt>
                  <dd className="text-ink">
                    {c.allowed_tools.join(" + ") || t("cred.command_and_http")} · {t("cred.limit", { n: c.max_uses_hour })}
                  </dd>
                </dl>
              )}
            </div>
          ))}
        </div>
      </Advanced>
    </div>
  );
}

// ------------------------------------------------------------------ by agent

function AgentCard({ a, groups, requests, act, highlight }: { a: RosterAgent; groups: Group[]; requests: CredRequest[]; act: Act; highlight: number | null }) {
  const held = groups.filter((g) => g.byAgent.has(a.id));
  const free = groups.filter((g) => !g.byAgent.has(a.id));
  const rec = free.filter((g) => g.recommended.some((r) => r.id === a.id));
  const [pick, setPick] = useState("");
  const grant = (g: Group) =>
    act(
      credentialsApi.grantMany(
        [a.id],
        g.creds.map((c) => c.name),
        null,
        null,
      ),
      t("cred.has", { name: a.name, title: g.title }),
    );
  return (
    <div className="panel flex min-w-0 flex-col gap-3 p-4">
      <div className="flex min-w-0 items-center gap-2">
        <Avatar name={a.name} size={26} />
        <div className="flex min-w-0 flex-col">
          <Link to={`/agents/${a.id}`} className="truncate text-[15px] font-medium hover:text-accent">
            {a.name}
          </Link>
          {a.purpose && <Label className="truncate">{a.purpose}</Label>}
        </div>
      </div>
      {requests.map((r) => (
        <RequestCard key={r.id} r={r} act={act} highlight={highlight === r.id} />
      ))}
      <div className="flex min-w-0 flex-wrap gap-1.5">
        {held.length === 0 && <Label>{t("cred.agent_none")}</Label>}
        {held.map((g) => {
          const e = g.byAgent.get(a.id)!;
          const grant0 = e.grants.find((x) => x.credential === g.primary.name) ?? e.grants[0];
          const ids = e.grants.map((x) => x.id);
          return (
            <span key={g.key} className="inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-full border border-line bg-raised py-0.5 pr-1 pl-2 text-xs">
              <KindIcon kind={g.kind} size={12} />
              <span className="truncate">{g.title}</span>
              <span className="shrink-0 text-ink-2">
                {grant0.scope ? `${t("cred.only", { scope: grant0.scope })} · ` : ""}
                {czLeft(grant0.expires_at)}
              </span>
              <button
                type="button"
                aria-label={t("cred.remove", { name: g.title })}
                title={t("cred.remove", { name: g.title })}
                className="rounded-full p-0.5 text-ink-2 hover:bg-line hover:text-red-400"
                onClick={() => act(credentialsApi.revokeMany(ids), t("cred.revoked", { name: a.name, title: g.title }), () => credentialsApi.restoreMany(ids))}
              >
                <X size={12} />
              </button>
            </span>
          );
        })}
      </div>
      {(rec.length > 0 || free.length > 0) && (
        <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
          {rec.map((g) => (
            <button
              key={g.key}
              type="button"
              className="inline-flex max-w-full min-w-0 items-center gap-1 rounded-full border border-dashed border-accent/60 px-2 py-0.5 text-accent hover:bg-accent/10"
              onClick={() => grant(g)}
              title={t("cred.rec_by_role")}
            >
              <Plus size={11} className="shrink-0" /> <span className="truncate">{g.title}</span>
            </button>
          ))}
          {free.length > rec.length && (
            <span className="flex min-w-0 items-center gap-1">
              <select aria-label={t("cred.add_for", { name: a.name })} value={pick} onChange={(e) => setPick(e.target.value)} className={`${field} max-w-52`}>
                <option value="">{t("cred.add_access")}</option>
                {free
                  .filter((g) => !rec.includes(g))
                  .map((g) => (
                    <option key={g.key} value={g.key}>
                      {g.title}
                    </option>
                  ))}
              </select>
              <button
                type="button"
                className="btn h-8!"
                disabled={!pick}
                onClick={() => {
                  const g = free.find((x) => x.key === pick);
                  if (g) grant(g).then(() => setPick(""));
                }}
              >
                <Plus size={13} /> {t("cred.add")}
              </button>
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function AgentsView({ v, groups, act, highlight }: { v: CredOverview; groups: Group[]; act: Act; highlight: number | null }) {
  const [others, setOthers] = useState(false);
  const active = v.agents.filter((a) => groups.some((g) => g.byAgent.has(a.id)) || v.requests.some((r) => r.agent_id === a.id));
  const rest = v.agents.filter((a) => !active.includes(a));
  const card = (a: RosterAgent) => (
    <AgentCard key={a.id} a={a} groups={groups} requests={v.requests.filter((r) => r.agent_id === a.id)} act={act} highlight={highlight} />
  );
  return (
    <div className="flex min-w-0 flex-col gap-3">
      {active.length === 0 && <p className="panel px-4 py-3 text-sm text-ink-2">{t("cred.nobody_anything")}</p>}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">{active.map(card)}</div>
      {rest.length > 0 && (
        <button type="button" className="self-start text-xs text-ink-2 hover:text-accent" onClick={() => setOthers(!others)}>
          {others ? t("cred.hide_others") : t("cred.others_count", { n: rest.length })}
        </button>
      )}
      {others && <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">{rest.map(card)}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ the page

function Tabs({ value, onChange, counts }: { value: string; onChange: (v: string) => void; counts: Record<string, number> }) {
  const tab = (k: string, name: ReactNode) => (
    <button
      type="button"
      role="tab"
      aria-selected={value === k}
      onClick={() => onChange(k)}
      className={`rounded px-3 py-1.5 text-sm ${value === k ? "bg-raised text-ink" : "text-ink-2 hover:text-ink"}`}
    >
      {name} <span className="text-xs text-ink-2">{counts[k]}</span>
    </button>
  );
  return (
    <div role="tablist" className="inline-flex max-w-full self-start rounded-md border border-line p-0.5">
      {tab("hesla", t("cred.tab.creds"))}
      {tab("agenti", t("cred.tab.agents"))}
    </div>
  );
}

export default function Credentials() {
  const [v, setV] = useState<CredOverview | null>(null);
  const [d, setD] = useState<Discovery | null>(null);
  const [dLoading, setDLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  const highlight = Number(params.get("request")) || null;
  const view = params.get("view") === "agenti" ? "agenti" : "hesla";

  const load = useCallback(() => {
    credentialsApi.overview().then(
      (o) => {
        setV(o);
        setLoadError(null);
      },
      (e) => setLoadError(errText(e)),
    );
  }, []);
  const loadDiscovery = useCallback((refresh = false, hidden = false) => {
    setDLoading(true);
    credentialsApi
      .discover(refresh, hidden)
      .then(setD, (e) => setD({ enabled: true, vault: null, reason: null, cache_seconds: 0, items: [], hidden: [], error: errText(e) }))
      .finally(() => setDLoading(false));
  }, []);
  const reloadAll = useCallback(() => {
    load();
    loadDiscovery(false);
  }, [load, loadDiscovery]);
  useEffect(() => {
    load();
    loadDiscovery(false);
  }, [load, loadDiscovery]);

  const act = useAct(reloadAll);

  const groups = useMemo(() => (v ? groupsOf(v) : []), [v]);
  const agents = v?.agents ?? [];

  return (
    <div className="flex min-w-0 flex-col gap-6 pb-20">
      <PageHeader kicker={t("settings.kicker")} title={t("nav.credentials")} sub={t("cred.sub")} />
      {v && !v.enabled && (
        <p className="panel border-amber-400/60! px-4 py-3 text-sm break-words text-amber-300">
          {t("cred.op_off", { reason: v.reason ?? "" })} <code>OP_SERVICE_ACCOUNT_TOKEN</code> {t("cred.and")} <code>POS_OP_VAULT</code> {t("cred.op_off_docs")}
        </p>
      )}
      {loadError && <p className="text-xs break-words text-red-400">{loadError}</p>}

      {v && v.requests.length > 0 && (
        <section className="flex min-w-0 flex-col gap-3">
          <h2 className="text-base font-medium">
            {t("cred.waiting")} <span className="text-xs text-ink-2">{v.requests.length}</span>
          </h2>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {v.requests.map((r) => (
              <RequestCard key={r.id} r={r} act={act} highlight={highlight === r.id} />
            ))}
          </div>
        </section>
      )}

      <DiscoverySection d={d} loading={dLoading} agents={agents} act={act} reload={loadDiscovery} />

      <section className="flex min-w-0 flex-col gap-3">
        <Tabs
          value={view}
          onChange={(k) => {
            const p = new URLSearchParams(params);
            if (k === "hesla") p.delete("view");
            else p.set("view", k);
            setParams(p, { replace: true });
          }}
          counts={{ hesla: groups.length, agenti: agents.filter((a) => groups.some((g) => g.byAgent.has(a.id))).length }}
        />
        {!v && !loadError && <p className="text-xs text-ink-2">{t("act.loading")}</p>}
        {v && view === "hesla" && (
          <>
            {groups.length === 0 && <p className="panel px-4 py-3 text-sm text-ink-2">{t("cred.empty")}</p>}
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {groups.map((g) => (
                <CredentialCard key={g.key} g={g} agents={agents} act={act} />
              ))}
            </div>
          </>
        )}
        {v && view === "agenti" && <AgentsView v={v} groups={groups} act={act} highlight={highlight} />}
      </section>

      <Panel title={t("cred.usage_7d")} right={t("cred.usage_7d_right")}>
        <AuditList lines={v?.audit ?? []} />
      </Panel>
    </div>
  );
}

// ------------------------------------------------------------------ on an agent's page

/** On an agent's page: what it holds (one click to take away, with undo), its requests, a quick add and its audit. */
export function AgentCredentialsPanel({ agentId }: { agentId: number }) {
  const [v, setV] = useState<CredOverview | null>(null);
  const [mine, setMine] = useState<Awaited<ReturnType<typeof credentialsApi.agent>> | null>(null);
  const load = useCallback(() => {
    credentialsApi.overview().then(setV, () => setV(null));
    credentialsApi.agent(agentId).then(setMine, () => setMine(null));
  }, [agentId]);
  useEffect(load, [load]);
  const act = useAct(load);
  const groups = useMemo(() => (v ? groupsOf(v) : []), [v]);
  if (!v || !mine) return null;
  const a = v.agents.find((x) => x.id === agentId);
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <div className="flex min-w-0 flex-col gap-2 lg:col-span-5">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="text-sm font-medium">{t("cred.panel_title")}</h2>
          <Link to="/credentials?view=agenti" className="ml-auto text-xs text-ink-2 hover:text-accent">
            {t("cred.all_on_page")}
          </Link>
        </div>
        {a ? <AgentCard a={a} groups={groups} requests={mine.requests} act={act} highlight={null} /> : <p className="text-xs text-ink-2">{t("cred.cannot_have")}</p>}
      </div>
      <Panel title={t("cred.usage_7d")} className="min-w-0 lg:col-span-7" bodyClassName="max-h-[380px] overflow-y-auto">
        <AuditList lines={mine.audit} />
      </Panel>
    </div>
  );
}
