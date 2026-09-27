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
import { PageHeader, Panel } from "../components/ui";

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const list = (s: string) => s.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean);

// ------------------------------------------------------------------ toast with undo

type Toast = { text: string; error?: boolean; undo?: () => Promise<unknown> };

function useToast() {
  const [toast, setToast] = useState<Toast | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const show = useCallback((t: Toast) => {
    setToast(t);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setToast(null), t.undo ? 10000 : t.error ? 9000 : 5000);
  }, []);
  return { toast, show, hide: () => setToast(null) };
}

function ToastBar({ toast, hide, reload }: { toast: Toast | null; hide: () => void; reload: () => void }) {
  if (!toast) return null;
  return (
    <div role="status" className="fixed inset-x-3 bottom-3 z-50 mx-auto flex max-w-xl items-center gap-3 rounded-md border border-line bg-raised px-4 py-3 text-sm shadow-2xl sm:bottom-6">
      <span className={`min-w-0 flex-1 ${toast.error ? "text-red-400" : ""}`}>{toast.text}</span>
      {toast.undo && (
        <button
          type="button"
          className="btn-accent h-7! shrink-0"
          onClick={() => {
            const u = toast.undo!;
            hide();
            u().then(reload, reload);
          }}
        >
          Vrátit
        </button>
      )}
      <button type="button" aria-label="Zavřít" className="shrink-0 text-ink-3 hover:text-ink" onClick={hide}>
        <X size={14} />
      </button>
    </div>
  );
}

type Act = (p: Promise<unknown>, ok?: string, undo?: () => Promise<unknown>) => Promise<boolean>;

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
  const [denying, setDenying] = useState(false);
  const [why, setWhy] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (highlight) ref.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [highlight]);
  const s = r.suggestion;
  const what = r.registered ? r.credential : s ? s.title : r.credential;
  const missing = !r.registered && !s;
  return (
    <div ref={ref} className={`panel flex flex-col gap-3 p-4 ${highlight ? "border-accent!" : "border-amber-400/50!"}`}>
      <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
        <Avatar name={r.agent_name} size={24} />
        <span className="min-w-0">
          <b className="font-medium">{r.agent_name}</b> chce <b className="font-medium text-accent">{what}</b>
          {r.scope ? <span className="text-ink-2"> (jen {r.scope})</span> : null}
          <span className="text-ink-2"> · {r.hours ? `na ${r.hours} h` : "natrvalo"}</span>
        </span>
        <span className="cap ml-auto shrink-0">{czAgo(r.created_at)}</span>
      </div>
      <div className="text-sm text-ink-2">
        <span className="cap mr-1">protože</span>
        <Markdown text={r.why} compact className="inline" />
        {r.task_ref && <span className="cap"> · úkol {r.task_ref}</span>}
      </div>
      {!r.registered && s && (
        <p className="flex items-start gap-2 rounded border border-accent/40 bg-accent/5 px-3 py-2 text-xs text-ink-2">
          <Sparkles size={13} className="mt-0.5 shrink-0 text-accent" />
          <span>
            Ještě není zaregistrované, ale je v 1Passwordu ({s.kind_label}). Schválením se zaregistruje jako{" "}
            <span className="font-mono text-accent">{s.credentials.map((c) => c.name).join(" + ")}</span>
            {s.extra_grants.length ? ` a agent dostane i nástroj ${s.extra_grants.map((x) => x.replace("tool:", "")).join(", ")}` : ""}.
          </span>
        </p>
      )}
      {missing && (
        <p className="rounded border border-amber-400/50 px-3 py-2 text-xs text-amber-300">
          Tohle v trezoru není. Přidej položku do trezoru PersonalOS v 1Passwordu, pak půjde schválit, nebo žádost zamítni.
        </p>
      )}
      {denying ? (
        <div className="flex flex-wrap items-center gap-2">
          <input autoFocus aria-label="Důvod zamítnutí" placeholder="Proč ne? (agent to uvidí, nepovinné)" value={why} onChange={(e) => setWhy(e.target.value)} className={`${field} min-w-0 flex-1`} />
          <button type="button" className="btn h-8!" onClick={() => act(credentialsApi.decide(r.id, "deny", why), `Zamítnuto, ${r.agent_name} dostane zprávu.`)}>
            Zamítnout
          </button>
          <button type="button" className="cap hover:text-ink!" onClick={() => setDenying(false)}>
            zpět
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className="btn-accent"
            disabled={missing}
            onClick={() => act(credentialsApi.decide(r.id, "grant", ""), `Schváleno: ${r.agent_name} má ${what} a pokračuje v práci.`)}
          >
            <Check size={14} /> Schválit
          </button>
          <button type="button" className="btn" onClick={() => setDenying(true)}>
            <X size={14} /> Zamítnout
          </button>
        </div>
      )}
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
      ids.length ? `${s.title}: zaregistrováno a přiděleno (${chosen.join(", ")}).` : `${s.title}: zaregistrováno.`,
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
          <span className="cap truncate">
            {s.kind_label}
            {s.known ? ` · ${s.known}` : ""}
            {s.source === "llm" ? " · návrh modelu" : !s.decided ? " · nejistý návrh" : ""}
          </span>
        </div>
        <button
          type="button"
          title="Skrýt (nechci registrovat)"
          aria-label="Skrýt"
          className="shrink-0 text-ink-3 hover:text-ink"
          onClick={() => act(credentialsApi.dismiss(s.item_id, true), `${s.title} skryto.`, () => credentialsApi.dismiss(s.item_id, false))}
        >
          <EyeOff size={14} />
        </button>
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs">
        <dt className="cap pt-0.5">Použití</dt>
        <dd className="min-w-0 break-words text-ink-2">{s.usage || "—"}</dd>
        <dt className="cap pt-0.5">Kam smí</dt>
        <dd className="min-w-0 break-words font-mono text-ink-2">{s.hosts.length ? s.hosts.join(", ") : <span className="font-sans text-ink-3">kamkoli, kam ho pustí příkaz</span>}</dd>
        <dt className="cap pt-0.5">Komu</dt>
        <dd className="flex min-w-0 flex-wrap gap-1">
          {pick.length === 0 ? (
            <span className="text-ink-3">nenašel jsem vhodného agenta — vyber v Upravit</span>
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
            <span className="cap">Co to je</span>
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
                  <span className="cap">{c.role === "user" ? "Název (uživatel)" : "Název"}</span>
                  <input value={c.name} onChange={(e) => setC(i, { name: e.target.value.toLowerCase().replace(/[^a-z0-9_.-]/g, "-") })} className={field} />
                </label>
                <label className="flex min-w-0 flex-col gap-1">
                  <span className="cap">Pole v 1Password</span>
                  <select value={c.op_ref} onChange={(e) => setC(i, { op_ref: e.target.value, field: s.fields.find((f) => f.op_ref === e.target.value)?.title ?? c.field })} className={field}>
                    {s.fields.map((f) => (
                      <option key={f.id} value={f.op_ref}>
                        {f.section ? `${f.section} / ` : ""}
                        {f.title} ({f.type})
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex min-w-0 flex-col gap-1 sm:col-span-2">
                  <span className="cap">Kam smí (hosty, čárkou)</span>
                  <input value={c.allowed_hosts.join(", ")} onChange={(e) => setC(i, { allowed_hosts: list(e.target.value) })} className={field} />
                </label>
              </div>
              <Advanced>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <label className="flex min-w-0 flex-col gap-1">
                    <span className="cap">Proměnná pro příkaz</span>
                    <input value={c.env_var ?? ""} onChange={(e) => setC(i, { env_var: e.target.value.toUpperCase() || null })} className={field} />
                  </label>
                  <label className="flex min-w-0 flex-col gap-1">
                    <span className="cap">HTTP hlavička</span>
                    <input value={c.header ?? ""} placeholder="Authorization: Bearer {value}" onChange={(e) => setC(i, { header: e.target.value || null })} className={field} />
                  </label>
                  <label className="flex min-w-0 flex-col gap-1">
                    <span className="cap">Povolené příkazy (čárkou)</span>
                    <input value={c.allowed_commands.join(", ")} onChange={(e) => setC(i, { allowed_commands: list(e.target.value) })} className={field} />
                  </label>
                  <label className="flex min-w-0 flex-col gap-1">
                    <span className="cap">Nástroje (command, http; prázdné = oba)</span>
                    <input value={c.allowed_tools.join(", ")} onChange={(e) => setC(i, { allowed_tools: list(e.target.value) })} className={field} />
                  </label>
                  <span className="cap break-all sm:col-span-2">{c.op_ref}</span>
                </div>
              </Advanced>
            </div>
          ))}
          <div className="flex flex-col gap-1 border-t border-line pt-2">
            <span className="cap">Komu přidělit</span>
            <AgentPicker agents={agents} recommended={s.agents} value={pick} onChange={setPick} />
          </div>
          <label className="flex items-center gap-2">
            <span className="cap">Na jak dlouho</span>
            <HoursPick value={hours} onChange={setHours} />
          </label>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn-accent" disabled={busy || none} onClick={() => apply(creds, pick, hours)}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
          {pick.length ? "Zaregistrovat a přidělit" : "Zaregistrovat"}
        </button>
        <button type="button" className="btn" onClick={() => setEditing(!editing)} aria-expanded={editing}>
          <Pencil size={13} /> {editing ? "Hotovo" : "Upravit"}
        </button>
        {none && <span className="cap">V položce není tajné pole.</span>}
      </div>
    </div>
  );
}

function DiscoverySection({ d, loading, agents, act, reload }: { d: Discovery | null; loading: boolean; agents: RosterAgent[]; act: Act; reload: (refresh: boolean, hidden?: boolean) => void }) {
  const [showHidden, setShowHidden] = useState(false);
  if (!d && loading) return <p className="cap flex items-center gap-2"><Loader2 size={12} className="animate-spin" /> Hledám nové položky v 1Passwordu…</p>;
  if (!d || !d.enabled) return null;
  const items = d.items;
  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-base font-medium">Nové v 1Password</h2>
        <span className="cap">{items.length ? `${items.length} čeká na registraci · návrhy podle názvu, polí a adres` : ""}</span>
        <span className="ml-auto flex items-center gap-3">
          {d.hidden.length > 0 && (
            <button type="button" className="cap hover:text-accent!" onClick={() => { setShowHidden(!showHidden); reload(false, !showHidden); }}>
              {showHidden ? "schovat skryté" : `skryté (${d.hidden.length})`}
            </button>
          )}
          <button type="button" className="cap flex items-center gap-1 hover:text-accent!" onClick={() => reload(true, showHidden)} disabled={loading}>
            <RefreshCw size={11} className={loading ? "animate-spin" : ""} /> načíst znovu
          </button>
        </span>
      </div>
      {d.error && <p className="cap text-red-400!">{d.error}</p>}
      {!d.error && items.length === 0 && (
        <p className="panel px-4 py-3 text-sm text-ink-2">
          Všechno z trezoru <b className="font-medium">{d.vault}</b> je zaregistrované. Přidej heslo nebo token do trezoru PersonalOS v 1Passwordu a objeví se tady i s návrhem, komu ho dát.
        </p>
      )}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {items.map((s) =>
          s.hidden ? (
            <div key={s.item_id} className="panel flex items-center gap-2 px-4 py-2 text-sm text-ink-3">
              <EyeOff size={13} /> <span className="min-w-0 flex-1 truncate">{s.title}</span>
              <button type="button" className="cap hover:text-accent!" onClick={() => act(credentialsApi.dismiss(s.item_id, false), `${s.title} je zase vidět.`)}>
                zobrazit
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
        <select aria-label="Rozsah" value={scope} onChange={(e) => setScope(e.target.value)} className={field}>
          <option value="">vše, co přístup dovolí</option>
          <option value="command">jen příkazy</option>
          <option value="http">jen HTTP</option>
          {g.primary.allowed_hosts.map((h) => (
            <option key={h} value={h}>
              jen {h}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="btn-accent"
          disabled={pick.length === 0}
          onClick={() => {
            const who = pick.map((id) => agents.find((a) => a.id === id)?.name).join(", ");
            act(credentialsApi.grantMany(pick, names, hours, scope || null), `${g.title}: přiděleno (${who}).`).then((ok) => ok && onDone());
          }}
        >
          <Plus size={14} /> Přidělit{pick.length > 1 ? ` (${pick.length})` : ""}
        </button>
        <button type="button" className="cap hover:text-ink!" onClick={onDone}>
          zrušit
        </button>
      </div>
      {(g.primary.companions?.length ?? 0) > 0 && <span className="cap">Agent dostane i nástroj {g.primary.companions!.map((x) => x.replace("tool:", "")).join(", ")}.</span>}
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
          `${c.name} uloženo.`,
        ).then((ok) => ok && onDone());
      }}
    >
      <label className="flex min-w-0 flex-col gap-1">
        <span className="cap">Proměnná pro příkaz</span>
        <input value={f.env_var} onChange={set("env_var")} className={field} />
      </label>
      <label className="flex min-w-0 flex-col gap-1">
        <span className="cap">HTTP hlavička</span>
        <input value={f.header} placeholder="Authorization: Bearer {value}" onChange={set("header")} className={field} />
      </label>
      <label className="flex min-w-0 flex-col gap-1">
        <span className="cap">Kam smí (hosty)</span>
        <input value={f.hosts} onChange={set("hosts")} className={field} />
      </label>
      <label className="flex min-w-0 flex-col gap-1">
        <span className="cap">Povolené příkazy</span>
        <input value={f.commands} onChange={set("commands")} className={field} />
      </label>
      <label className="flex min-w-0 flex-col gap-1">
        <span className="cap">Nástroje (command, http)</span>
        <input value={f.tools} onChange={set("tools")} className={field} />
      </label>
      <label className="flex min-w-0 flex-col gap-1">
        <span className="cap">Max. použití za hodinu</span>
        <input value={f.max} onChange={(e) => setF({ ...f, max: e.target.value.replace(/[^0-9]/g, "") })} className={field} />
      </label>
      <label className="flex min-w-0 flex-col gap-1 sm:col-span-2">
        <span className="cap">Popis (agenti ho vidí)</span>
        <textarea value={f.description} onChange={set("description")} rows={2} className={`${field} h-auto! py-1`} />
      </label>
      <div className="flex gap-2 sm:col-span-2">
        <button className="btn-accent h-7!">
          <Check size={13} /> Uložit
        </button>
        <button type="button" className="btn h-7!" onClick={onDone}>
          Zrušit
        </button>
      </div>
    </form>
  );
}

function StatusLine({ g }: { g: Group }) {
  const last = g.creds.map((c) => c.last_use).filter(Boolean).sort((a, b) => (a!.at < b!.at ? 1 : -1))[0];
  const tests = g.creds.map((c) => c.last_test).filter(Boolean);
  const failed = tests.find((t) => !t!.ok);
  const test = failed ?? tests.sort((a, b) => (a!.at < b!.at ? 1 : -1))[0];
  const errors = g.creds.reduce((n, c) => n + (c.errors_24h ?? 0), 0);
  const uses = g.creds.reduce((n, c) => n + (c.uses_24h ?? 0), 0);
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-2">
      <span>
        {last ? (
          <>
            Naposledy {czAgo(last.at)}
            {last.agent ? ` · ${last.agent}` : ""} · <span className={last.ok ? "" : "text-red-400"}>{last.ok ? "OK" : "odmítnuto"}</span>
          </>
        ) : (
          <span className="text-ink-3">Zatím nepoužito</span>
        )}
      </span>
      {uses > 0 && <span className="cap">{uses}× za 24 h</span>}
      {errors > 0 && <span className="rounded border border-red-400/50 px-1.5 text-xs text-red-400">{errWord(errors)} za 24 h</span>}
      {test && (
        <span className={`cap ${test.ok ? "text-accent!" : "text-red-400!"}`} title={test.error ?? undefined}>
          {test.ok ? `test OK ${czAgo(test.at)}` : `test selhal ${czAgo(test.at)}: ${test.error ?? ""}`}
        </span>
      )}
    </div>
  );
}

function CredentialCard({ g, agents, act }: { g: Group; agents: RosterAgent[]; act: Act }) {
  const [granting, setGranting] = useState(false);
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [editing, setEditing] = useState<number | null>(null);
  const [testing, setTesting] = useState(false);
  const removeAgent = (agentId: number, name: string, grants: CredGrant[]) => {
    const ids = grants.map((x) => x.id);
    act(credentialsApi.revokeMany(ids), `${name} už nemá ${g.title}.`, () => credentialsApi.restoreMany(ids));
  };
  const test = () => {
    setTesting(true);
    act(
      Promise.all(g.creds.map((c) => credentialsApi.test(c.id).then((r) => ({ c, r })))).then((rs) => {
        const bad = rs.filter((x) => !x.r.ok);
        if (bad.length) throw new Error(`Test selhal: ${bad.map((x) => `${x.c.name}: ${x.r.error}`).join("; ")}`);
      }),
      `${g.title}: test OK, hodnota se z 1Passwordu načetla (nikde se nezobrazuje).`,
    ).finally(() => setTesting(false));
  };
  return (
    <div className="panel flex min-w-0 flex-col gap-3 p-4">
      <div className="flex min-w-0 items-start gap-2">
        <KindIcon kind={g.kind} size={17} />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="truncate text-[15px] font-medium">{g.title}</span>
          <span className="cap truncate">
            {KIND_LABEL[g.kind]} · <span className="font-mono">{g.creds.map((c) => c.name).join(" + ")}</span>
          </span>
        </div>
      </div>
      {g.primary.description && <Markdown text={g.primary.description} compact className="text-xs text-ink-2" />}
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        {g.byAgent.size === 0 && g.paused.length === 0 && <span className="text-xs text-ink-3">Zatím ho nemá žádný agent.</span>}
        {[...g.byAgent.entries()].map(([id, e]) => (
          <AgentChip key={id} name={e.name} grant={e.grants.find((x) => x.credential === g.primary.name) ?? e.grants[0]} onRemove={() => removeAgent(id, e.name, e.grants)} />
        ))}
        {g.paused.map((p) => (
          <AgentChip key={p.id} name={p.agent_name} paused onResume={() => act(credentialsApi.resume(p.id), `${p.agent_name}: přístup obnoven.`)} />
        ))}
      </div>
      <StatusLine g={g} />
      {granting && <GrantBox g={g} agents={agents} act={act} onDone={() => setGranting(false)} />}
      {confirmArchive ? (
        <div className="flex flex-wrap items-center gap-2 rounded border border-red-400/50 px-3 py-2 text-xs">
          <span className="min-w-0 flex-1">Archivovat {g.title}? Všem agentům přístup skončí. Heslo v 1Passwordu zůstane.</span>
          <button type="button" className="btn h-7! hover:text-red-400!" onClick={() => act(Promise.all(g.creds.map((c) => credentialsApi.archive(c.id, "archivováno majitelem"))), `${g.title} archivováno.`)}>
            Archivovat
          </button>
          <button type="button" className="cap hover:text-ink!" onClick={() => setConfirmArchive(false)}>
            zpět
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {!granting && (
            <button type="button" className="btn-accent h-8!" onClick={() => setGranting(true)}>
              <Plus size={14} /> Přidělit
            </button>
          )}
          <button type="button" className="btn h-8!" disabled={testing} onClick={test}>
            {testing ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />} Otestovat
          </button>
          <button type="button" className="btn h-8!" onClick={() => setConfirmArchive(true)}>
            <Archive size={13} /> Archivovat
          </button>
        </div>
      )}
      <Advanced>
        <div className="flex flex-col gap-3">
          {g.creds.map((c) => (
            <div key={c.id} className="flex min-w-0 flex-col gap-1 border-t border-line pt-2 text-xs">
              <div className="flex min-w-0 items-center gap-2">
                <span className="font-mono text-accent">{c.name}</span>
                <button type="button" className="cap ml-auto hover:text-accent!" onClick={() => setEditing(editing === c.id ? null : c.id)}>
                  {editing === c.id ? "zavřít" : "upravit"}
                </button>
              </div>
              {editing === c.id ? (
                <CredEdit c={c} act={act} onDone={() => setEditing(null)} />
              ) : (
                <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5">
                  <dt className="cap">1Password</dt>
                  <dd className="font-mono break-all text-ink-2">{c.op_ref}</dd>
                  <dt className="cap">proměnná</dt>
                  <dd className="font-mono text-ink-2">{c.env_var ?? `CRED_${c.name.toUpperCase().replace(/[^A-Z0-9]/g, "_")}`}</dd>
                  <dt className="cap">hlavička</dt>
                  <dd className="font-mono break-all text-ink-2">{c.header ?? "—"}</dd>
                  <dt className="cap">hosty</dt>
                  <dd className="font-mono break-all text-ink-2">{c.allowed_hosts.join(", ") || "—"}</dd>
                  <dt className="cap">příkazy</dt>
                  <dd className="font-mono break-all text-ink-2">{c.allowed_commands.join(", ") || "—"}</dd>
                  <dt className="cap">nástroje</dt>
                  <dd className="text-ink-2">{c.allowed_tools.join(" + ") || "příkaz i HTTP"} · limit {c.max_uses_hour}/h</dd>
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
  const grant = (g: Group) => act(credentialsApi.grantMany([a.id], g.creds.map((c) => c.name), null, null), `${a.name} má ${g.title}.`);
  return (
    <div className="panel flex min-w-0 flex-col gap-3 p-4">
      <div className="flex min-w-0 items-center gap-2">
        <Avatar name={a.name} size={26} />
        <div className="flex min-w-0 flex-col">
          <Link to={`/agents/${a.id}`} className="truncate text-[15px] font-medium hover:text-accent">
            {a.name}
          </Link>
          {a.purpose && <span className="cap truncate">{a.purpose}</span>}
        </div>
      </div>
      {requests.map((r) => (
        <RequestCard key={r.id} r={r} act={act} highlight={highlight === r.id} />
      ))}
      <div className="flex min-w-0 flex-wrap gap-1.5">
        {held.length === 0 && <span className="text-xs text-ink-3">Nemá žádné heslo ani token.</span>}
        {held.map((g) => {
          const e = g.byAgent.get(a.id)!;
          const grant0 = e.grants.find((x) => x.credential === g.primary.name) ?? e.grants[0];
          const ids = e.grants.map((x) => x.id);
          return (
            <span key={g.key} className="inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-full border border-line bg-raised py-0.5 pr-1 pl-2 text-xs">
              <KindIcon kind={g.kind} size={12} />
              <span className="truncate">{g.title}</span>
              <span className="cap shrink-0">{grant0.scope ? `jen ${grant0.scope} · ` : ""}{czLeft(grant0.expires_at)}</span>
              <button type="button" aria-label={`Odebrat ${g.title}`} className="rounded-full p-0.5 text-ink-3 hover:bg-line hover:text-red-400" onClick={() => act(credentialsApi.revokeMany(ids), `${a.name} už nemá ${g.title}.`, () => credentialsApi.restoreMany(ids))}>
                <X size={12} />
              </button>
            </span>
          );
        })}
      </div>
      {(rec.length > 0 || free.length > 0) && (
        <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
          {rec.map((g) => (
            <button key={g.key} type="button" className="inline-flex items-center gap-1 rounded-full border border-dashed border-accent/60 px-2 py-0.5 text-accent hover:bg-accent/10" onClick={() => grant(g)} title="Doporučeno podle role agenta">
              <Plus size={11} /> {g.title}
            </button>
          ))}
          {free.length > rec.length && (
            <span className="flex min-w-0 items-center gap-1">
              <select aria-label={`Přidat přístup pro ${a.name}`} value={pick} onChange={(e) => setPick(e.target.value)} className={`${field} max-w-52`}>
                <option value="">přidat přístup…</option>
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
                <Plus size={13} /> Přidat
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
    <div className="flex flex-col gap-3">
      {active.length === 0 && <p className="panel px-4 py-3 text-sm text-ink-2">Zatím nikdo nic nemá. Přiděl přístup na kartě hesla nebo níže u agenta.</p>}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">{active.map(card)}</div>
      {rest.length > 0 && (
        <button type="button" className="cap self-start hover:text-accent!" onClick={() => setOthers(!others)}>
          {others ? "Schovat ostatní agenty" : `Ostatní agenti bez přístupů (${rest.length})`}
        </button>
      )}
      {others && <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">{rest.map(card)}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ the page

function Tabs({ value, onChange, counts }: { value: string; onChange: (v: string) => void; counts: Record<string, number> }) {
  const tab = (k: string, label: ReactNode) => (
    <button
      type="button"
      role="tab"
      aria-selected={value === k}
      onClick={() => onChange(k)}
      className={`rounded px-3 py-1.5 text-sm ${value === k ? "bg-raised text-ink" : "text-ink-3 hover:text-ink"}`}
    >
      {label} <span className="cap">{counts[k]}</span>
    </button>
  );
  return (
    <div role="tablist" className="inline-flex self-start rounded-md border border-line p-0.5">
      {tab("hesla", "Hesla a tokeny")}
      {tab("agenti", "Podle agentů")}
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
  const { toast, show, hide } = useToast();

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

  const act: Act = useCallback(
    async (p, ok, undo) => {
      try {
        await p;
        if (ok) show({ text: ok, undo });
        reloadAll();
        return true;
      } catch (e) {
        show({ text: errText(e), error: true });
        reloadAll();
        return false;
      }
    },
    [show, reloadAll],
  );

  const groups = useMemo(() => (v ? groupsOf(v) : []), [v]);
  const agents = v?.agents ?? [];

  return (
    <div className="flex min-w-0 flex-col gap-6 pb-20">
      <PageHeader
        kicker="PŘÍSTUPY · 1PASSWORD"
        title="Přístupy"
        sub="Hesla a tokeny pro agenty. Agent zná jen název: hodnotu PersonalOS vezme z 1Passwordu až ve chvíli použití a z výstupu ji začerní. Přidělit je můžeš jen ty."
      />
      {v && !v.enabled && (
        <p className="panel border-amber-400/60! px-4 py-3 text-sm text-amber-300">
          1Password není připojený ({v.reason}). Dokud to nebude, každé použití hesla selže. Na serveru chybí <code>OP_SERVICE_ACCOUNT_TOKEN</code> a <code>POS_OP_VAULT</code> (návod v docs/CREDENTIALS.md).
        </p>
      )}
      {loadError && <p className="cap text-red-400!">{loadError}</p>}

      {v && v.requests.length > 0 && (
        <section className="flex flex-col gap-3">
          <h2 className="text-base font-medium">
            Čeká na tebe <span className="cap">{v.requests.length}</span>
          </h2>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {v.requests.map((r) => (
              <RequestCard key={r.id} r={r} act={act} highlight={highlight === r.id} />
            ))}
          </div>
        </section>
      )}

      <DiscoverySection d={d} loading={dLoading} agents={agents} act={act} reload={loadDiscovery} />

      <section className="flex flex-col gap-3">
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
        {!v && !loadError && <p className="cap">Načítám…</p>}
        {v && view === "hesla" && (
          <>
            {groups.length === 0 && (
              <p className="panel px-4 py-3 text-sm text-ink-2">
                Zatím tu nic není. Přidej heslo do trezoru PersonalOS v 1Passwordu a objeví se nahoře v „Nové v 1Password“ i s návrhem, komu ho dát.
              </p>
            )}
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
              {groups.map((g) => (
                <CredentialCard key={g.key} g={g} agents={agents} act={act} />
              ))}
            </div>
          </>
        )}
        {v && view === "agenti" && <AgentsView v={v} groups={groups} act={act} highlight={highlight} />}
      </section>

      <Panel title="Použití za 7 dní" right="každé načtení: kdo, co, kdy; chyby nahoře červeně">
        <AuditList lines={v?.audit ?? []} />
      </Panel>
      <ToastBar toast={toast} hide={hide} reload={reloadAll} />
    </div>
  );
}

// ------------------------------------------------------------------ on an agent's page

/** On an agent's page: what it holds (one click to take away, with undo), its requests, a quick add and its audit. */
export function AgentCredentialsPanel({ agentId }: { agentId: number }) {
  const [v, setV] = useState<CredOverview | null>(null);
  const [mine, setMine] = useState<Awaited<ReturnType<typeof credentialsApi.agent>> | null>(null);
  const { toast, show, hide } = useToast();
  const load = useCallback(() => {
    credentialsApi.overview().then(setV, () => setV(null));
    credentialsApi.agent(agentId).then(setMine, () => setMine(null));
  }, [agentId]);
  useEffect(load, [load]);
  const act: Act = useCallback(
    async (p, ok, undo) => {
      try {
        await p;
        if (ok) show({ text: ok, undo });
        load();
        return true;
      } catch (e) {
        show({ text: errText(e), error: true });
        return false;
      }
    },
    [show, load],
  );
  const groups = useMemo(() => (v ? groupsOf(v) : []), [v]);
  if (!v || !mine) return null;
  const a = v.agents.find((x) => x.id === agentId);
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <div className="flex min-w-0 flex-col gap-2 lg:col-span-5">
        <div className="flex items-baseline gap-2">
          <span className="cap text-accent!">PŘÍSTUPY</span>
          <h2 className="text-sm font-medium">Hesla a tokeny</h2>
          <Link to="/credentials?view=agenti" className="cap ml-auto hover:text-accent!">
            vše na stránce Přístupy →
          </Link>
        </div>
        {a ? (
          <AgentCard a={a} groups={groups} requests={mine.requests} act={act} highlight={null} />
        ) : (
          <p className="cap">Tento agent nemůže mít hesla (člověk, služba nebo Správce přístupů).</p>
        )}
      </div>
      <Panel title="Použití za 7 dní" className="lg:col-span-7" bodyClassName="max-h-[380px] overflow-y-auto">
        <AuditList lines={mine.audit} />
      </Panel>
      <ToastBar toast={toast} hide={hide} reload={load} />
    </div>
  );
}
