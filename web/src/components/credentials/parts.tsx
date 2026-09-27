import { AlertTriangle, ChevronDown, ChevronRight, Database, Globe, KeyRound, Terminal, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import type { Agent } from "../../agentsApi";
import { type AuditLine, type CredGrant, type CredKind, type CredUse, credentialsApi } from "../../credentialsApi";

export const field = "h-8 min-w-0 rounded border border-line bg-bg px-2 font-mono text-xs outline-none focus:border-accent";

export const KIND_LABEL: Record<CredKind, string> = {
  ssh: "SSH přihlášení",
  token: "API token",
  basic: "HTTP basic",
  db: "Databáze",
  generic: "Obecné heslo",
};

export function KindIcon({ kind, size = 15 }: { kind?: CredKind; size?: number }) {
  const cls = "shrink-0 text-accent";
  if (kind === "ssh") return <Terminal size={size} className={cls} />;
  if (kind === "db") return <Database size={size} className={cls} />;
  if (kind === "basic") return <Globe size={size} className={cls} />;
  return <KeyRound size={size} className={cls} />;
}

/** "před 5 min", "před 2 h", "před 3 dny". */
export function czAgo(iso: string | null | undefined) {
  if (!iso) return "nikdy";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "právě teď";
  if (s < 3600) return `před ${Math.round(s / 60)} min`;
  if (s < 86400) return `před ${Math.round(s / 3600)} h`;
  const d = Math.round(s / 86400);
  return `před ${d} ${d === 1 ? "dnem" : "dny"}`;
}

/** "ještě 3 h", "ještě 2 dny". */
export function czLeft(iso: string | null | undefined) {
  if (!iso) return "natrvalo";
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  if (s < 3600) return `ještě ${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `ještě ${Math.round(s / 3600)} h`;
  const d = Math.round(s / 86400);
  return `ještě ${d} ${d === 1 ? "den" : d < 5 ? "dny" : "dní"}`;
}

export function dayLabel(day: string) {
  const today = new Date();
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  if (day === iso(today)) return "dnes";
  const y = new Date(today.getTime() - 86400000);
  if (day === iso(y)) return "včera";
  const [, m, d] = day.split("-");
  return `${Number(d)}. ${Number(m)}.`;
}

export const times = (n: number) => `${n}×`;
export const errWord = (n: number) => (n === 1 ? "1 chyba" : n < 5 ? `${n} chyby` : `${n} chyb`);

export const HOURS: { label: string; value: number | null }[] = [
  { label: "natrvalo", value: null },
  { label: "na 1 hodinu", value: 1 },
  { label: "na 8 hodin", value: 8 },
  { label: "na 1 den", value: 24 },
  { label: "na 7 dní", value: 168 },
];

export function HoursPick({ value, onChange }: { value: number | null; onChange: (v: number | null) => void }) {
  return (
    <select aria-label="Na jak dlouho" value={value ?? ""} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)} className={field}>
      {HOURS.map((h) => (
        <option key={h.label} value={h.value ?? ""}>
          {h.label}
        </option>
      ))}
    </select>
  );
}

function hue(name: string) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

export function initials(name: string) {
  const w = name.replace(/[&]/g, " ").split(/\s+/).filter(Boolean);
  return (w.length > 1 ? w[0][0] + w[1][0] : name.slice(0, 2)).toUpperCase();
}

export function Avatar({ name, size = 20 }: { name: string; size?: number }) {
  return (
    <span
      aria-hidden
      className="inline-flex shrink-0 items-center justify-center rounded-full font-mono text-[9px] font-medium text-bg"
      style={{ width: size, height: size, background: `hsl(${hue(name)} 45% 68%)` }}
    >
      {initials(name)}
    </span>
  );
}

/** An agent holding a credential: avatar, name, scope and time left; × takes it away (with undo). */
export function AgentChip({ name, grant, onRemove, paused, onResume }: { name: string; grant?: CredGrant; onRemove?: () => void; paused?: boolean; onResume?: () => void }) {
  const extra = [grant?.scope ? `jen ${grant.scope}` : null, grant?.expires_at ? czLeft(grant.expires_at) : null].filter(Boolean).join(" · ");
  return (
    <span
      className={`inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-full border py-0.5 pr-1 pl-0.5 text-xs ${paused ? "border-amber-400/60" : "border-line bg-raised"}`}
      title={grant ? `${grant.reason}${grant.granted_by_name ? ` — ${grant.granted_by_name}` : ""}` : undefined}
    >
      <Avatar name={name} />
      <span className="truncate">{name}</span>
      {extra && <span className="cap shrink-0">{extra}</span>}
      {paused && (
        <button type="button" className="cap shrink-0 text-amber-300! hover:underline" onClick={onResume}>
          pozastaveno · obnovit
        </button>
      )}
      {onRemove && !paused && (
        <button type="button" aria-label={`Odebrat ${name}`} title={`Odebrat ${name}`} onClick={onRemove} className="shrink-0 rounded-full p-0.5 text-ink-3 hover:bg-line hover:text-red-400">
          <X size={12} />
        </button>
      )}
    </span>
  );
}

/** Tick agents: the recommended ones on top (with why), the rest below. */
export function AgentPicker({ agents, recommended, value, onChange, exclude = [] }: { agents: { id: number; name: string }[]; recommended: { id: number; name: string; why: string }[]; value: number[]; onChange: (ids: number[]) => void; exclude?: number[] }) {
  const [all, setAll] = useState(false);
  const rec = recommended.filter((r) => !exclude.includes(r.id));
  const recIds = new Set(rec.map((r) => r.id));
  const rest = agents.filter((a) => !recIds.has(a.id) && !exclude.includes(a.id));
  const toggle = (id: number) => onChange(value.includes(id) ? value.filter((x) => x !== id) : [...value, id]);
  const chip = (a: { id: number; name: string }, why?: string) => {
    const on = value.includes(a.id);
    return (
      <button
        key={a.id}
        type="button"
        aria-pressed={on}
        onClick={() => toggle(a.id)}
        className={`inline-flex max-w-full min-w-0 items-center gap-1.5 rounded-full border py-0.5 pr-2 pl-0.5 text-xs ${on ? "border-accent bg-accent/15 text-accent" : "border-line text-ink-2 hover:border-accent"}`}
        title={why}
      >
        <Avatar name={a.name} />
        <span className="truncate">{a.name}</span>
        {why && <span className="cap hidden truncate sm:inline">· {why}</span>}
      </button>
    );
  };
  return (
    <div className="flex flex-col gap-2">
      {rec.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="cap">Doporučení</span>
          <div className="flex flex-wrap gap-1.5">{rec.map((a) => chip(a, a.why))}</div>
        </div>
      )}
      {rest.length > 0 && (
        <div className="flex flex-col gap-1">
          {rec.length ? (
            <button type="button" className="cap flex items-center gap-1 self-start hover:text-accent!" onClick={() => setAll(!all)}>
              {all ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              {`Ostatní agenti (${rest.length})`}
            </button>
          ) : (
            <span className="cap">Vyber agenty</span>
          )}
          {(all || rec.length === 0) && <div className="flex max-h-44 flex-wrap gap-1.5 overflow-y-auto">{rest.map((a) => chip(a))}</div>}
        </div>
      )}
    </div>
  );
}

/** A small collapsible "Pokročilé" block. */
export function Advanced({ children, label = "Pokročilé" }: { children: ReactNode; label?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-2">
      <button type="button" className="cap flex items-center gap-1 self-start hover:text-accent!" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />} {label}
      </button>
      {open && children}
    </div>
  );
}

/** The grouped audit: one line per day, credential and agent; errors first in red; expand for the uses. */
export function AuditList({ lines, showCredential = true, limit = 12 }: { lines: AuditLine[]; showCredential?: boolean; limit?: number }) {
  const [all, setAll] = useState(false);
  if (lines.length === 0) return <p className="cap px-4 py-4">Za posledních 7 dní nikdo nic nepoužil. Každé načtení hesla se sem zapíše: kdo, co, kdy a s jakým výsledkem.</p>;
  const shown = all ? lines : lines.slice(0, limit);
  return (
    <div className="flex flex-col">
      {shown.map((l) => (
        <AuditRow key={`${l.day}-${l.name}-${l.agent_id}`} l={l} showCredential={showCredential} />
      ))}
      {lines.length > limit && (
        <button type="button" className="cap px-4 py-2 text-left hover:text-accent!" onClick={() => setAll(!all)}>
          {all ? "Méně" : `Zobrazit vše (${lines.length})`}
        </button>
      )}
    </div>
  );
}

function AuditRow({ l, showCredential }: { l: AuditLine; showCredential: boolean }) {
  const [open, setOpen] = useState(false);
  const [uses, setUses] = useState<CredUse[] | null>(null);
  const who = l.agent_name ?? "majitel (test)";
  const toggle = () => {
    if (!open && uses === null) credentialsApi.uses({ name: l.name, agent_id: l.agent_id ?? 0, day: l.day }).then((r) => setUses(r.uses), () => setUses([]));
    setOpen(!open);
  };
  const bad = l.errors > 0;
  return (
    <div className={`border-b border-line last:border-0 ${bad ? "bg-red-500/5" : ""}`}>
      <button type="button" onClick={toggle} aria-expanded={open} className="flex w-full min-w-0 items-center gap-2 px-4 py-2 text-left text-xs hover:bg-raised">
        {open ? <ChevronDown size={12} className="shrink-0 text-ink-3" /> : <ChevronRight size={12} className="shrink-0 text-ink-3" />}
        {bad && <AlertTriangle size={12} className="shrink-0 text-red-400" />}
        <span className="min-w-0 flex-1 break-words">
          {showCredential && <span className="font-mono text-accent">{l.name}</span>}
          {showCredential && <span className="text-ink-3"> · </span>}
          <span>{who}</span>
          <span className="text-ink-3"> · </span>
          <span className="text-ink-2">
            {times(l.count)} {dayLabel(l.day)}
          </span>
          <span className="text-ink-3">, </span>
          {bad ? <span className="text-red-400">{errWord(l.errors)}</span> : <span className="text-ink-2">vše OK</span>}
        </span>
        <span className="cap hidden shrink-0 sm:inline">{l.tools.join(", ")}</span>
      </button>
      {open && (
        <div className="flex flex-col gap-0.5 px-4 pb-2 pl-9 text-xs">
          {bad && l.last_error && <p className="text-red-400">Poslední chyba: {l.last_error}</p>}
          {l.hosts.length > 0 && <p className="cap">hosty: {l.hosts.join(", ")}</p>}
          {uses === null && <p className="cap">Načítám…</p>}
          {uses?.map((u) => (
            <p key={u.id} className={`flex min-w-0 gap-2 ${u.ok ? "text-ink-2" : "text-red-400"}`}>
              <span className="cap shrink-0">{new Date(u.at).toLocaleTimeString("cs-CZ", { hour: "2-digit", minute: "2-digit" })}</span>
              <span className="min-w-0 truncate" title={u.error ?? undefined}>
                {u.ok ? "OK" : "odmítnuto"} · {u.tool}
                {u.host ? ` · ${u.host}` : ""}
                {u.task_ref ? ` · ${u.task_ref}` : ""}
                {u.error ? ` — ${u.error}` : ""}
              </span>
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

export type AgentLite = Pick<Agent, "id" | "name">;
