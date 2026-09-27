import { useEffect, useState } from "react";
import { api } from "../api";
import { Panel } from "./ui";

type Window = { name: string; used_percent: number; projected_percent: number; hours_to_reset: number | null };
type Budget = {
  at: string;
  level: "ok" | "watch" | "throttle" | "pause";
  reasons: string[];
  windows: Window[];
  period: { used_tokens: number; projected_tokens: number; budget_tokens: number | null } | null;
  caps: Record<string, number>;
};
type Member = { id: number; name: string; kind: string; archived: boolean; budget_class: string | null; tokens_24h: number };

const LEVEL: Record<Budget["level"], string> = { ok: "text-accent", watch: "text-amber-300", throttle: "text-amber-300", pause: "text-red-400" };
const fmt = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : String(n));

/** Rozpočtář on the System page: the Codex subscription's level and windows,
 *  the caps per class, and each agent's class (the Claude side is in Runtimes). */
export default function BudgetPanel() {
  const [b, setB] = useState<Budget | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = () => {
    api<Budget>("/api/budget").then(setB, (e) => setError(e.message));
    api<{ agents: Member[] }>("/api/agents").then((d) => setMembers(d.agents.filter((a) => a.kind !== "human" && !a.archived)), () => undefined);
  };
  useEffect(load, []);
  const setClass = (id: number, cls: string) =>
    api(`/api/budget/agents/${id}`, { method: "PUT", body: JSON.stringify({ budget_class: cls }) }).then(load, (e) => setError(e.message));
  return (
    <Panel
      title="Budget"
      right={
        <span className="flex items-center gap-2">
          {b && <span className={`${LEVEL[b.level]}!`}>{b.level}</span>}
          <button type="button" className="hover:text-accent" onClick={() => api<Budget>("/api/budget/check", { method: "POST" }).then(setB, (e) => setError(e.message))}>
            check now
          </button>
        </span>
      }
    >
      {b && (
        <div className="flex flex-col gap-1.5 border-b border-line px-4 py-3">
          {(b.reasons ?? []).map((r) => (
            <span key={r} className="text-[13px]">
              {r}
            </span>
          ))}
          {(b.windows ?? []).map((w) => (
            <div key={w.name} className="flex flex-col gap-1">
              <span className="cap">
                Codex {w.name} window · {Math.round(w.used_percent)} % used · projected {Math.round(w.projected_percent)} %
                {w.hours_to_reset != null ? ` · resets in ${Math.round(w.hours_to_reset)} h` : ""}
              </span>
              <span className="relative h-1.5 rounded-[1px] bg-line">
                <span className={`absolute inset-y-0 left-0 ${w.used_percent >= 90 ? "bg-red-400" : w.used_percent >= 70 ? "bg-amber-300" : "bg-ink"}`} style={{ width: `${Math.min(100, w.used_percent)}%` }} />
              </span>
            </div>
          ))}
          {b.period && (
            <span className="cap">
              this month {fmt(b.period.used_tokens)} tokens · projected {fmt(b.period.projected_tokens)}
              {b.period.budget_tokens ? ` of ${fmt(b.period.budget_tokens)}` : ""}
            </span>
          )}
          {Object.keys(b.caps ?? {}).length > 0 && (
            <span className="cap">
              daily caps: {Object.entries(b.caps).map(([k, v]) => `${k} ${fmt(v)}`).join(" · ")}
            </span>
          )}
        </div>
      )}
      {members.map((m) => (
        <div key={m.id} className="flex items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0">
          <span className="min-w-0 flex-1 truncate">{m.name}</span>
          <span className="cap">{fmt(m.tokens_24h)} / 24 h</span>
          <select
            aria-label={`Budget class of ${m.name}`}
            value={m.budget_class ?? "normal"}
            onChange={(e) => setClass(m.id, e.target.value)}
            className="h-7 rounded border border-line bg-bg px-1 font-mono text-xs outline-none focus:border-accent"
          >
            {["system", "normal", "low"].map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </div>
      ))}
      {error && <p className="cap px-4 py-2 text-red-400!">{error}</p>}
    </Panel>
  );
}
