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
              className="ml-auto text-ink-3 hover:text-red-400"
              title="Odebrat"
              onClick={() => {
                const why = window.prompt(`Proč odebrat ${g.credential} agentovi ${g.agent_name}?`)?.trim();
                if (why !== undefined) act(credentialsApi.revoke(g.id, why || "odebráno majitelem"));
              }}
            >
              <X size={13} />
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

function slug(s: string) {
  return s.toLowerCase().normalize("NFKD").replace(/[^\w\s.-]/g, "").trim().replace(/[\s_]+/g, "-").slice(0, 60);
}

/** Add (or edit) a registry entry. */
function CredentialForm({ initial, onSave, onCancel }: { initial?: Credential; onSave: (c: CredentialIn) => void; onCancel?: () => void }) {
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
    });
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 p-4 text-xs">
      {!initial && (
        <VaultPicker
          onPick={(fld, it) =>
            setF({ ...f, op_ref: fld.op_ref, name: f.name || slug(`${it.title}`), description: f.description || `${it.title} (${fld.title})` })
          }
        />
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
      <div className="flex gap-2">
        <button className="btn">
          <Check size={13} /> {initial ? "Uložit" : "Přidat do registru"}
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

/** Grant one credential to an agent (owner only). */
function GrantForm({ cred, agents, act }: { cred: Credential; agents: Agent[]; act: (p: Promise<unknown>) => void }) {
  const [agent, setAgent] = useState("");
  const [hours, setHours] = useState("");
  const [scope, setScope] = useState("");
  const [reason, setReason] = useState("");
  return (
    <form
      className="flex flex-wrap items-end gap-2 px-4 py-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (!agent || !reason.trim()) return;
        act(credentialsApi.grant(Number(agent), cred.name, reason.trim(), hours ? Number(hours) : null, scope || null));
        setReason("");
      }}
    >
      <select aria-label="Agent" value={agent} onChange={(e) => setAgent(e.target.value)} className={field}>
        <option value="">agent…</option>
        {agents.map((a) => (
          <option key={a.id} value={a.id}>
            {a.name}
          </option>
        ))}
      </select>
      <select aria-label="Rozsah" value={scope} onChange={(e) => setScope(e.target.value)} className={field}>
        <option value="">jakékoli povolené použití</option>
        <option value="command">jen příkaz</option>
        <option value="http">jen HTTP</option>
        {cred.allowed_hosts.map((h) => (
          <option key={h} value={h}>
            jen {h}
          </option>
        ))}
      </select>
      <input aria-label="Hodiny" placeholder="hodin (prázdné = natrvalo)" value={hours} onChange={(e) => setHours(e.target.value.replace(/[^0-9.]/g, ""))} className={`${field} w-44`} />
      <input aria-label="Důvod" placeholder="důvod" value={reason} onChange={(e) => setReason(e.target.value)} className={`${field} min-w-40 flex-1`} />
      <button className="btn">
        <Plus size={13} /> Udělit
      </button>
    </form>
  );
}

function CredentialCard({ c, agents, act, open, setOpen }: { c: Credential; agents: Agent[]; act: (p: Promise<unknown>, ok?: string) => void; open: boolean; setOpen: (v: boolean) => void }) {
  const [editing, setEditing] = useState(false);
  const [detail, setDetail] = useState<{ grants: CredGrant[]; uses: CredUse[] } | null>(null);
  useEffect(() => {
    if (open) credentialsApi.detail(c.id).then(setDetail, () => setDetail(null));
  }, [open, c.id, c.updated_at, c.grants?.length]);
  const reAct = (p: Promise<unknown>, ok?: string) => {
    act(p.then((x) => (credentialsApi.detail(c.id).then(setDetail), x)), ok);
  };
  return (
    <div className="border-b border-line">
      <button className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-[13px] hover:bg-line/30" onClick={() => setOpen(!open)}>
        <KeyRound size={14} className="text-accent" />
        <span className="font-mono">{c.name}</span>
        <span className="cap truncate">{c.op_ref}</span>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {(c.grants ?? []).length > 0 && <Pill>{`${c.grants!.length} agent${c.grants!.length === 1 ? "" : "i"}`}</Pill>}
          <span className="cap">{c.uses_24h ?? 0}× / 24 h</span>
        </span>
      </button>
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
            <GrantForm cred={c} agents={agents} act={reAct} />
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
              onCancel={() => setAdding(false)}
              onSave={(c) => {
                act(credentialsApi.add(c).then(() => setAdding(false)));
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

/** On an agent's page: its credential grants and uses. */
export function AgentCredentialsPanel({ agentId }: { agentId: number }) {
  const [v, setV] = useState<Awaited<ReturnType<typeof credentialsApi.agent>> | null>(null);
  const load = useCallback(() => {
    credentialsApi.agent(agentId).then(setV, () => setV(null));
  }, [agentId]);
  useEffect(load, [load]);
  const { error, act } = useAct(load);
  if (!v || (v.grants.length === 0 && v.uses.length === 0)) return null;
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <Panel fig="PŘÍSTUPY" title="Hesla a tokeny" right={v.enabled ? "jen názvy, nikdy hodnoty" : "1Password vypnuto"} className="lg:col-span-5" bodyClassName="max-h-[300px] overflow-y-auto">
        <GrantRows items={v.grants} act={act} byAgent={false} />
      </Panel>
      <Panel fig="AUDIT" title="Použití" className="lg:col-span-7" bodyClassName="max-h-[300px] overflow-y-auto">
        <UseLog items={v.uses} />
      </Panel>
      {error && <p className="cap text-red-400! lg:col-span-12">{error}</p>}
    </div>
  );
}
