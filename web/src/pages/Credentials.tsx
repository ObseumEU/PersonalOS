import { Check, KeyRound, Pause, Play, Plus, RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { agentsApi, type Agent } from "../agentsApi";
import { type CredGrant, type CredOverview, type CredUse, type Credential, type CredentialIn, credentialsApi, type VaultField, type VaultItem } from "../credentialsApi";
import Markdown from "../components/Markdown";
import { until } from "../components/Schedules";
import { Pill } from "../components/agents/bits";
import { PageHeader, Panel } from "../components/ui";
import { ago } from "./Agents";

const field = "h-7 rounded border border-line bg-bg px-2 font-mono text-xs outline-none focus:border-accent";
const list = (s: string) => s.split(/[\n,]+/).map((x) => x.trim()).filter(Boolean);

function useAct(reload: () => void) {
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const act = async (p: Promise<unknown>, ok?: string) => {
    try {
      await p;
      setError(null);
      setNote(ok ?? null);
      reload();
    } catch (e) {
      setNote(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return { error, note, act };
}

/** Who used which credential, when, for what; refusals in red. */
export function UseLog({ items, showCredential = true }: { items: CredUse[]; showCredential?: boolean }) {
  if (items.length === 0) return <p className="cap px-4 py-3">Zatím žádné použití.</p>;
  return (
    <>
      {items.map((u) => (
        <div key={u.id} className="grid grid-cols-[80px_minmax(0,1fr)_minmax(0,1.4fr)] gap-2 border-b border-line px-4 py-1.5 text-xs" title={u.error ?? undefined}>
          <span className="cap">{ago(u.at)}</span>
          <span className="truncate">
            {showCredential && <span className="font-mono text-accent">{u.name}</span>}
            {showCredential && " · "}
            {u.agent_name ?? "majitel (test)"}
          </span>
          <span className={`truncate ${u.ok ? "text-ink-2" : "text-red-400"}`}>
            {u.ok ? "OK" : "odmítnuto"} · {u.tool}
            {u.host ? ` · ${u.host}` : ""}
            {u.task_ref ? ` · ${u.task_ref}` : ""}
            {u.run_id ? ` · běh #${u.run_id}` : ""}
            {u.error ? ` — ${u.error}` : ""}
          </span>
        </div>
      ))}
    </>
  );
}

/** Grants of one credential or one agent, with revoke and resume. */
export function GrantRows({ items, act, byAgent = true }: { items: CredGrant[]; act: (p: Promise<unknown>) => void; byAgent?: boolean }) {
  if (items.length === 0) return <p className="cap px-4 py-2">Žádné přístupy.</p>;
  return (
    <>
      {items.map((g) => (
        <div key={g.id} className={`flex items-center gap-2 border-b border-line px-4 py-1.5 text-xs ${g.active ? "" : "opacity-60"}`} title={`${g.reason}${g.end_reason ? ` — ${g.end_reason}` : ""}`}>
          <span className="font-mono text-accent">{byAgent ? g.agent_name : g.credential}</span>
          {g.scope && <Pill>{g.scope}</Pill>}
          {g.end_kind === "paused" && <Pill warn>pozastaveno</Pill>}
          <span className="cap truncate">
            {g.active ? (g.expires_at ? `vyprší ${until(g.expires_at)}` : "natrvalo") : `${g.end_kind ?? "ukončeno"} ${ago(g.ended_at)}`}
          </span>
          {g.active ? (
            <button
              className="btn ml-auto h-6! px-2! hover:text-red-400"
              title={`Odebrat ${g.credential} agentovi ${g.agent_name}`}
              onClick={() => {
                const why = window.prompt(`Proč odebrat ${g.credential} agentovi ${g.agent_name}? (nepovinné)`)?.trim();
                if (why !== undefined) act(credentialsApi.revoke(g.id, why || "odebráno majitelem"));
              }}
            >
              <X size={12} /> Odebrat
            </button>
          ) : g.end_kind === "paused" ? (
            <button className="btn ml-auto h-6! px-2!" title="Obnovit přístup" onClick={() => act(credentialsApi.resume(g.id))}>
              <Play size={12} /> Obnovit
            </button>
          ) : null}
        </div>
      ))}
    </>
  );
}

/** The owner picks an item's field from the 1Password vault; the reference is filled in, never a value. */
function VaultPicker({ onPick }: { onPick: (f: VaultField, item: VaultItem) => void }) {
  const [items, setItems] = useState<VaultItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => {
    setError(null);
    credentialsApi.vault().then((v) => setItems(v.items), (e) => setError(e instanceof Error ? e.message : String(e)));
  };
  return (
    <div className="flex flex-col gap-1 border-b border-line px-4 py-2">
      <button type="button" className="btn self-start" onClick={load}>
        <RefreshCw size={12} /> Načíst položky z 1Password
      </button>
      {error && <p className="cap text-red-400!">{error}</p>}
      {items && items.length === 0 && <p className="cap">Trezor je prázdný.</p>}
      <div className="max-h-56 overflow-y-auto">
        {items?.map((it) => (
          <div key={it.id} className="py-1 text-xs">
            <span className="text-ink-2">{it.title}</span> <span className="cap">{it.category}</span>
            <div className="flex flex-wrap gap-1 pt-1">
              {it.fields.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  disabled={f.registered}
                  className={`rounded border border-line px-1.5 py-0.5 font-mono text-[11px] ${f.registered ? "opacity-40" : "hover:border-accent"}`}
                  title={f.registered ? "Už je v registru" : f.op_ref}
                  onClick={() => onPick(f, it)}
                >
                  {f.section ? `${f.section}/` : ""}
                  {f.title} <span className="text-ink-3">{f.type}</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

const EMPTY = { name: "", op_ref: "", description: "", env_var: "", header: "", hosts: "", commands: "", tools: "", max: "60", notes: "" };

/** "SSH HomeAssistant" + "password" -> "ssh-homeassistant-password" (a-z0-9 and dashes only). */
export function slug(s: string) {
  return s
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60)
    .replace(/-+$/, "");
}

/** "ssh-homeassistant-password" -> "SSH_HOMEASSISTANT_PASSWORD". */
const envOf = (name: string) => name.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "");

/** Tick one or more agents (chips). */
export function AgentMultiPick({ agents, value, onChange }: { agents: Agent[]; value: number[]; onChange: (ids: number[]) => void }) {
  if (agents.length === 0) return <span className="cap">Žádní agenti.</span>;
  return (
    <div className="flex max-h-32 flex-wrap gap-1 overflow-y-auto">
      {agents.map((a) => {
        const on = value.includes(a.id);
        return (
          <button
            key={a.id}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(on ? value.filter((x) => x !== a.id) : [...value, a.id])}
            className={`rounded border px-1.5 py-0.5 text-[11px] ${on ? "border-accent bg-accent/15 text-accent" : "border-line text-ink-2 hover:border-accent"}`}
          >
            {on ? "✓ " : ""}
            {a.name}
          </button>
        );
      })}
    </div>
  );
}

/** Scope picker: vše / jen příkazy / jen HTTP / one host (from the credential's allowed hosts or typed). */
function ScopePick({ hosts, value, onChange }: { hosts: string[]; value: string; onChange: (v: string) => void }) {
  const preset = value === "" || value === "command" || value === "http" || hosts.includes(value);
  const [custom, setCustom] = useState(!preset);
  return (
    <span className="flex items-center gap-1">
      <select
        aria-label="Rozsah"
        value={custom ? "__host" : value}
        onChange={(e) => {
          if (e.target.value === "__host") {
            setCustom(true);
            onChange("");
          } else {
            setCustom(false);
            onChange(e.target.value);
          }
        }}
        className={field}
      >
        <option value="">vše povolené</option>
        <option value="command">jen příkazy</option>
        <option value="http">jen HTTP</option>
        {hosts.map((h) => (
          <option key={h} value={h}>
            jen {h}
          </option>
        ))}
        <option value="__host">jiný host…</option>
      </select>
      {custom && <input aria-label="Host" placeholder="host, např. api.example.com" value={value} onChange={(e) => onChange(e.target.value.trim())} className={`${field} w-48`} />}
    </span>
  );
}

/** Grant one credential to several agents, one after another; returns the names that failed. */
async function grantMany(ids: number[], name: string, reason: string, hours: number | null, scope: string | null, agents: Agent[]) {
  const failed: string[] = [];
  for (const id of ids) {
    try {
      await credentialsApi.grant(id, name, reason, hours, scope);
    } catch (e) {
      failed.push(`${agents.find((a) => a.id === id)?.name ?? `#${id}`}: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
  if (failed.length) throw new Error(`Přidělení selhalo — ${failed.join("; ")}`);
}

/** Add (or edit) a registry entry. */
function CredentialForm({ initial, agents = [], onSave, onCancel }: { initial?: Credential; agents?: Agent[]; onSave: (c: CredentialIn, agentIds: number[]) => void; onCancel?: () => void }) {
  const [pick, setPick] = useState<number[]>([]);
  const [f, setF] = useState(
    initial
      ? {
          name: initial.name, op_ref: initial.op_ref, description: initial.description, env_var: initial.env_var ?? "",
          header: initial.header ?? "", hosts: initial.allowed_hosts.join(", "), commands: initial.allowed_commands.join("\n"),
          tools: initial.allowed_tools.join(", "), max: String(initial.max_uses_hour), notes: initial.notes,
        }
      : EMPTY,
  );
  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    onSave({
      ...(initial ? {} : { name: f.name.trim() }),
      op_ref: f.op_ref.trim(), description: f.description, env_var: f.env_var.trim(), header: f.header.trim(),
      allowed_hosts: list(f.hosts), allowed_commands: list(f.commands.replace(/,/g, "\n")), allowed_tools: list(f.tools),
      max_uses_hour: Number(f.max) || 60, notes: f.notes,
    }, initial ? [] : pick);
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 p-4 text-xs">
      {!initial && (
        <VaultPicker
          onPick={(fld, it) => {
            const name = slug(`${it.title} ${fld.section ? `${fld.section} ` : ""}${fld.title}`);
            setF({
              ...f,
              op_ref: fld.op_ref,
              name,
              // keep what the owner typed; replace what an earlier pick filled in
              env_var: !f.env_var || f.env_var === envOf(f.name) ? envOf(name) : f.env_var,
              tools: "",
              description: !f.description || /^.+ \(.+\)$/.test(f.description) ? `${it.title} (${fld.title})` : f.description,
            });
          }}
        />
      )}
      {!initial && f.op_ref && (
        <p className="cap">
          Vybráno: <span className="font-mono text-accent!">{f.op_ref}</span> → název <span className="font-mono">{f.name || "—"}</span>
        </p>
      )}
      <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <label className="flex flex-col gap-1">
          <span className="cap">Název (agenti ho používají)</span>
          <input disabled={!!initial} value={f.name} onChange={set("name")} placeholder="github-deploy" className={field} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">Odkaz do 1Password</span>
          <input value={f.op_ref} onChange={set("op_ref")} placeholder="op://PersonalOS Agents/GitHub deploy/token" className={field} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">Proměnná pro příkaz</span>
          <input value={f.env_var} onChange={set("env_var")} placeholder="GITHUB_TOKEN" className={field} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">HTTP hlavička</span>
          <input value={f.header} onChange={set("header")} placeholder="Authorization: Bearer {value}" className={field} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">Povolené hosty</span>
          <input value={f.hosts} onChange={set("hosts")} placeholder="api.github.com, github.com" className={field} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">Nástroje (prázdné = příkaz i HTTP)</span>
          <input value={f.tools} onChange={set("tools")} placeholder="command, http" className={field} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">Povolené příkazy (začátek, jeden na řádek)</span>
          <textarea value={f.commands} onChange={set("commands")} placeholder={"git push\ngh api"} rows={2} className={`${field} h-auto! py-1`} />
        </label>
        <label className="flex flex-col gap-1">
          <span className="cap">Max. použití za hodinu (na agenta)</span>
          <input value={f.max} onChange={(e) => setF({ ...f, max: e.target.value.replace(/[^0-9]/g, "") })} className={`${field} w-28`} />
        </label>
      </div>
      <label className="flex flex-col gap-1">
        <span className="cap">Popis (Markdown)</span>
        <textarea value={f.description} onChange={set("description")} rows={2} className={`${field} h-auto! py-1`} />
      </label>
      <label className="flex flex-col gap-1">
        <span className="cap">Poznámky majitele (Markdown)</span>
        <textarea value={f.notes} onChange={set("notes")} rows={2} className={`${field} h-auto! py-1`} />
      </label>
      {!initial && (
        <div className="flex flex-col gap-1">
          <span className="cap">Přidělit agentům (nepovinné; natrvalo, lze kdykoli odebrat)</span>
          <AgentMultiPick agents={agents} value={pick} onChange={setPick} />
        </div>
      )}
      <div className="flex gap-2">
        <button className="btn">
          <Check size={13} /> {initial ? "Uložit" : pick.length ? `Přidat a přidělit (${pick.length})` : "Přidat do registru"}
        </button>
        {onCancel && (
          <button type="button" className="btn" onClick={onCancel}>
            Zrušit
          </button>
        )}
        <span className="cap self-center">Hodnota se nikdy neukládá ani nezobrazuje: jen odkaz op://…</span>
      </div>
    </form>
  );
}

/** Grant one credential to one or more agents (owner only). */
function GrantForm({ cred, agents, act, onDone }: { cred: Credential; agents: Agent[]; act: (p: Promise<unknown>, ok?: string) => void; onDone?: () => void }) {
  const [pick, setPick] = useState<number[]>([]);
  const [hours, setHours] = useState("");
  const [scope, setScope] = useState("");
  const [reason, setReason] = useState("");
  const has = new Set((cred.grants ?? []).filter((g) => g.active).map((g) => g.agent_id));
  const free = agents.filter((a) => !has.has(a.id));
  return (
    <form
      className="flex flex-col gap-2 px-4 py-2 text-xs"
      onSubmit={(e) => {
        e.preventDefault();
        if (pick.length === 0) return;
        const names = pick.map((id) => agents.find((a) => a.id === id)?.name ?? `#${id}`).join(", ");
        act(
          grantMany(pick, cred.name, reason.trim() || "přiděleno majitelem", hours ? Number(hours) : null, scope || null, agents),
          `${cred.name} přiděleno: ${names}`,
        );
        setPick([]);
        setReason("");
        onDone?.();
      }}
    >
      <span className="cap">Komu přidělit {cred.name}</span>
      <AgentMultiPick agents={free} value={pick} onChange={setPick} />
      <div className="flex flex-wrap items-end gap-2">
        <ScopePick hosts={cred.allowed_hosts} value={scope} onChange={setScope} />
        <input aria-label="Hodiny" placeholder="hodin (prázdné = natrvalo)" value={hours} onChange={(e) => setHours(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-44`} />
        <input aria-label="Důvod" placeholder="důvod (nepovinné)" value={reason} onChange={(e) => setReason(e.target.value)} className={`${field} min-w-40 flex-1`} />
        <button className="btn" disabled={pick.length === 0}>
          <Plus size={13} /> Přidělit{pick.length > 1 ? ` (${pick.length})` : ""}
        </button>
      </div>
    </form>
  );
}

function CredentialCard({ c, agents, act, open, setOpen }: { c: Credential; agents: Agent[]; act: (p: Promise<unknown>, ok?: string) => void; open: boolean; setOpen: (v: boolean) => void }) {
  const [editing, setEditing] = useState(false);
  const [detail, setDetail] = useState<{ grants: CredGrant[]; uses: CredUse[] } | null>(null);
  useEffect(() => {
    if (open) credentialsApi.detail(c.id).then(setDetail, () => setDetail(null));
  }, [open, c.id, c.updated_at, c.grants?.length]);
  const [granting, setGranting] = useState(false);
  const active = (c.grants ?? []).filter((g) => g.active);
  const reAct = (p: Promise<unknown>, ok?: string) => {
    act(p.finally(() => credentialsApi.detail(c.id).then(setDetail, () => undefined)), ok);
  };
  return (
    <div className="border-b border-line">
      <div
        role="button"
        tabIndex={0}
        aria-expanded={open}
        className="flex w-full cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-left text-[13px] hover:bg-line/30"
        onClick={() => setOpen(!open)}
        onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), setOpen(!open))}
      >
        <KeyRound size={14} className="text-accent" />
        <span className="font-mono">{c.name}</span>
        <span className="cap min-w-0 flex-1 truncate">{c.op_ref}</span>
        <span className="flex shrink-0 flex-wrap items-center gap-2">
          {active.length === 0 ? (
            <span className="cap">nikdo nemá přístup</span>
          ) : (
            active.map((g) => <Pill key={g.id}>{g.scope ? `${g.agent_name} · ${g.scope}` : g.agent_name}</Pill>)
          )}
          <span className="cap">{c.uses_24h ?? 0}× / 24 h</span>
          <button
            className="btn h-6! px-2!"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(true);
              setGranting(true);
            }}
          >
            <Plus size={12} /> Přidělit
          </button>
        </span>
      </div>
      {open && granting && (
        <div className="mx-4 mb-3 rounded border border-accent/50">
          <GrantForm cred={c} agents={agents} act={reAct} onDone={() => setGranting(false)} />
        </div>
      )}
      {open && (
        <div className="grid grid-cols-1 gap-3 px-4 pb-4 lg:grid-cols-2">
          <div className="flex flex-col gap-2 text-xs">
            {c.description && <Markdown text={c.description} compact />}
            <span className="cap">
              {c.env_var ? `proměnná ${c.env_var}` : "proměnná CRED_…"} · {c.header ? c.header.split(":")[0] : "bez hlavičky"} · hosty {c.allowed_hosts.join(", ") || "—"} ·
              příkazy {c.allowed_commands.join(", ") || "—"} · {c.allowed_tools.join("+") || "příkaz i HTTP"} · limit {c.max_uses_hour}/h
            </span>
            {c.notes && <Markdown text={c.notes} compact className="text-ink-2" />}
            <div className="flex flex-wrap gap-2">
              <button className="btn" onClick={() => act(credentialsApi.test(c.id).then((r) => { if (!r.ok) throw new Error(`Test selhal: ${r.error}`); }), `${c.name}: test OK (hodnota se nezobrazuje)`)}>
                <Check size={12} /> Otestovat načtení
              </button>
              <button className="btn" onClick={() => setEditing(!editing)}>
                Upravit
              </button>
              <button
                className="btn"
                onClick={() => {
                  const why = window.prompt(`Odebrat ${c.name} z registru? Všechny přístupy k němu skončí. Důvod:`)?.trim();
                  if (why) act(credentialsApi.archive(c.id, why));
                }}
              >
                <Pause size={12} /> Archivovat
              </button>
            </div>
            {editing && <CredentialForm initial={c} onCancel={() => setEditing(false)} onSave={(v) => { act(credentialsApi.update(c.id, v)); setEditing(false); }} />}
          </div>
          <div className="flex flex-col rounded border border-line">
            <span className="cap border-b border-line px-4 py-1.5">Přístupy agentů</span>
            <GrantRows items={detail?.grants ?? c.grants ?? []} act={reAct} />
            {!granting && <GrantForm cred={c} agents={agents} act={reAct} />}
            <span className="cap border-t border-b border-line px-4 py-1.5">Použití</span>
            <div className="max-h-56 overflow-y-auto">
              <UseLog items={detail?.uses ?? []} showCredential={false} />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function Credentials() {
  const [v, setV] = useState<CredOverview | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [params] = useSearchParams();
  const highlight = Number(params.get("request")) || null;
  const load = useCallback(() => {
    credentialsApi.overview().then(setV, () => setV(null));
  }, []);
  useEffect(load, [load]);
  useEffect(() => {
    agentsApi.list().then((r) => setAgents(r.agents.filter((a) => a.kind !== "human" && a.status !== "archived")), () => setAgents([]));
  }, []);
  const { error, note, act } = useAct(load);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="PŘÍSTUPY · CREDENTIALS · 1PASSWORD"
        title="Přístupy"
        sub="Hesla a tokeny pro agenty. Agent zná jen název; hodnotu PersonalOS načte z 1Password až při spuštění, vloží ji do jednoho příkazu nebo požadavku a z výstupu ji začerní. Přístup uděluje jen majitel."
      />
      {v && !v.enabled && (
        <p className="panel border-amber-400/60! px-4 py-3 text-sm text-amber-300">
          Vypnuto: {v.reason}. Každé použití teď selže (nic se nenačítá z prostého textu). Nastav na serveru{" "}
          <code>OP_SERVICE_ACCOUNT_TOKEN</code> a <code>POS_OP_VAULT</code> (docs/CREDENTIALS.md).
        </p>
      )}
      {error && <p className="cap text-red-400!">{error}</p>}
      {note && <p className="cap text-accent!">{note}</p>}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel
          fig="REGISTR"
          title="Registr přístupů"
          right={v ? `${v.credentials.length} · trezor ${v.vault ?? "—"} · cache ${v.cache_seconds} s` : ""}
          className="lg:col-span-8"
        >
          <div className="flex border-b border-line px-4 py-2">
            <button className="btn" onClick={() => setAdding(!adding)}>
              <Plus size={13} /> Přidat z 1Password
            </button>
          </div>
          {adding && (
            <CredentialForm
              agents={agents}
              onCancel={() => setAdding(false)}
              onSave={(c, ids) => {
                act(
                  credentialsApi.add(c).then(async (saved) => {
                    setAdding(false);
                    if (ids.length) await grantMany(ids, saved.name ?? c.name ?? "", "přiděleno majitelem při registraci", null, null, agents);
                  }),
                  ids.length ? `${c.name} přidáno a přiděleno (${ids.length})` : `${c.name} přidáno do registru`,
                );
              }}
            />
          )}
          {v?.credentials.length === 0 && <p className="cap px-4 py-3">Registr je prázdný.</p>}
          {v?.credentials.map((c) => (
            <CredentialCard key={c.id} c={c} agents={agents} act={act} open={open === c.id} setOpen={(o) => setOpen(o ? c.id : null)} />
          ))}
        </Panel>

        <div className="flex flex-col gap-4 lg:col-span-4">
          <Panel fig="ŽÁDOSTI" title="Žádosti agentů" right={v ? `${v.requests.length} čeká` : ""}>
            {v?.requests.length === 0 && <p className="cap px-4 py-3">Nic nečeká.</p>}
            {v?.requests.map((r) => (
              <div key={r.id} className={`flex flex-col gap-1 border-b border-line px-4 py-2 ${highlight === r.id ? "bg-accent/10" : ""}`}>
                <span className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="cap">#{r.id}</span>
                  <span>{r.agent_name}</span>
                  <span className="font-mono text-accent">{r.capability}</span>
                  {r.hours ? <Pill>{`${r.hours} h`}</Pill> : <Pill>natrvalo</Pill>}
                  {r.task_ref && <span className="cap">{r.task_ref}</span>}
                  <span className="cap ml-auto">{ago(r.created_at)}</span>
                </span>
                <Markdown text={r.why} compact className="text-xs text-ink-2" />
                <span className="flex gap-2">
                  <button className="btn h-6! px-2!" onClick={() => act(credentialsApi.decide(r.id, "grant", ""), `#${r.id} schváleno`)}>
                    <Check size={12} /> Schválit
                  </button>
                  <button
                    className="btn h-6! px-2!"
                    onClick={() => {
                      const why = window.prompt(`Proč zamítnout #${r.id}?`)?.trim();
                      if (why !== undefined) act(credentialsApi.decide(r.id, "deny", why ?? ""));
                    }}
                  >
                    <X size={12} /> Zamítnout
                  </button>
                </span>
              </div>
            ))}
          </Panel>
          <Panel fig="POZASTAVENO" title="Pozastavené přístupy" right="příliš mnoho použití za hodinu">
            <GrantRows items={v?.paused.filter((g) => !g.active) ?? []} act={act} />
          </Panel>
        </div>

        <Panel fig="AUDIT" title="Použití přístupů" right="každé načtení: kdo, co, kdy, k čemu" className="lg:col-span-12" bodyClassName="max-h-[360px] overflow-y-auto">
          <UseLog items={v?.uses ?? []} />
        </Panel>
      </div>
    </div>
  );
}

/** On an agent's page: its credential grants and uses, and the owner grants another registered credential here. */
export function AgentCredentialsPanel({ agentId }: { agentId: number }) {
  const [v, setV] = useState<Awaited<ReturnType<typeof credentialsApi.agent>> | null>(null);
  const [creds, setCreds] = useState<Credential[]>([]);
  const load = useCallback(() => {
    credentialsApi.agent(agentId).then(setV, () => setV(null));
    credentialsApi.overview().then((o) => setCreds(o.credentials), () => setCreds([]));
  }, [agentId]);
  useEffect(load, [load]);
  const { error, note, act } = useAct(load);
  const [name, setName] = useState("");
  const [scope, setScope] = useState("");
  const [hours, setHours] = useState("");
  const [reason, setReason] = useState("");
  if (!v) return null;
  const held = new Set(v.grants.filter((g) => g.active).map((g) => g.credential));
  const choices = creds.filter((c) => !held.has(c.name));
  const chosen = creds.find((c) => c.name === name);
  const grant = (e: FormEvent) => {
    e.preventDefault();
    if (!name) return;
    act(credentialsApi.grant(agentId, name, reason.trim() || "přiděleno majitelem", hours ? Number(hours) : null, scope || null), `${name} přiděleno`).then(() => {
      setName("");
      setScope("");
      setHours("");
      setReason("");
    });
  };
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <Panel fig="PŘÍSTUPY" title="Hesla a tokeny" right={v.enabled ? "jen názvy, nikdy hodnoty" : "1Password vypnuto"} className="lg:col-span-5" bodyClassName="max-h-[380px] overflow-y-auto">
        <GrantRows items={v.grants} act={act} byAgent={false} />
        <form onSubmit={grant} className="flex flex-col gap-2 border-t border-line p-3 text-xs">
          <span className="cap">Přidat přístup</span>
          {creds.length === 0 ? (
            <span className="cap">
              Registr je prázdný — nejdřív přidej položku na stránce <a href="/credentials" className="text-accent underline">Přístupy</a>.
            </span>
          ) : (
            <div className="flex flex-wrap items-end gap-2">
              <select aria-label="Přístup" value={name} onChange={(e) => { setName(e.target.value); setScope(""); }} className={field}>
                <option value="">vyber přístup…</option>
                {choices.map((c) => (
                  <option key={c.id} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </select>
              <ScopePick key={name} hosts={chosen?.allowed_hosts ?? []} value={scope} onChange={setScope} />
              <input aria-label="Hodiny" placeholder="hodin (prázdné = natrvalo)" value={hours} onChange={(e) => setHours(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-44`} />
              <input aria-label="Důvod" placeholder="důvod (nepovinné)" value={reason} onChange={(e) => setReason(e.target.value)} className={`${field} min-w-32 flex-1`} />
              <button className="btn" disabled={!name}>
                <Plus size={13} /> Přidat přístup
              </button>
            </div>
          )}
        </form>
      </Panel>
      <Panel fig="AUDIT" title="Použití" className="lg:col-span-7" bodyClassName="max-h-[380px] overflow-y-auto">
        <UseLog items={v.uses} />
      </Panel>
      {error && <p className="cap text-red-400! lg:col-span-12">{error}</p>}
      {note && <p className="cap text-accent! lg:col-span-12">{note}</p>}
    </div>
  );
}
