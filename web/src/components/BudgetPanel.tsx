import { useEffect, useState } from "react";
import { api } from "../api";
import { label, t } from "../i18n";
import { toast } from "./overlay";
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
const fmt = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1).replace(".", ",")} M` : n >= 1e3 ? `${Math.round(n / 1e3)} tis.` : String(n));
const CLASSES = ["system", "normal", "low"];

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
  const put = (id: number, cls: string) => api(`/api/budget/agents/${id}`, { method: "PUT", body: JSON.stringify({ budget_class: cls }) });
  const setClass = (m: Member, cls: string) => {
    const prev = m.budget_class ?? "normal";
    put(m.id, cls).then(
      () => {
        load();
        toast(t("budget.class_toast", { name: m.name, cls: label("budget.class", cls) }), {
          undo: () => put(m.id, prev).then(load, (e) => setError(e.message)),
        });
      },
      (e) => setError(e.message),
    );
  };
  return (
    <Panel
      title={t("budget.title")}
      right={
        <span className="flex items-center gap-2">
          {b && <span className={LEVEL[b.level]}>{label("budget.level", b.level)}</span>}
          <button type="button" className="hover:text-accent" onClick={() => api<Budget>("/api/budget/check", { method: "POST" }).then(setB, (e) => setError(e.message))}>
            {t("budget.check")}
          </button>
        </span>
      }
    >
      {b && (
        <div className="flex min-w-0 flex-col gap-1.5 border-b border-line px-4 py-3">
          {(b.reasons ?? []).map((r) => (
            <span key={r} className="text-[13px] break-words">
              {r}
            </span>
          ))}
          {(b.windows ?? []).map((w) => (
            <div key={w.name} className="flex flex-col gap-1">
              <span className="text-xs text-ink-2">
                {t("budget.window", { name: w.name, used: Math.round(w.used_percent), projected: Math.round(w.projected_percent) })}
                {w.hours_to_reset != null ? t("budget.resets", { h: Math.round(w.hours_to_reset) }) : ""}
              </span>
              <span className="relative h-1.5 rounded-[1px] bg-line">
                <span
                  className={`absolute inset-y-0 left-0 ${w.used_percent >= 90 ? "bg-red-400" : w.used_percent >= 70 ? "bg-amber-300" : "bg-ink"}`}
                  style={{ width: `${Math.min(100, w.used_percent)}%` }}
                />
              </span>
            </div>
          ))}
          {b.period && (
            <span className="text-xs text-ink-2">
              {t("budget.month", { used: fmt(b.period.used_tokens), projected: fmt(b.period.projected_tokens) })}
              {b.period.budget_tokens ? t("budget.of", { budget: fmt(b.period.budget_tokens) }) : ""}
            </span>
          )}
          {Object.keys(b.caps ?? {}).length > 0 && (
            <span className="text-xs break-words text-ink-2">
              {t("budget.caps", { caps: Object.entries(b.caps).map(([k, v]) => `${label("budget.class", k)} ${fmt(v)}`).join(" · ") })}
            </span>
          )}
        </div>
      )}
      {members.map((m) => (
        <div key={m.id} className="flex min-w-0 items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0">
          <span className="min-w-0 flex-1 truncate">{m.name}</span>
          <span className="shrink-0 text-xs text-ink-2">{t("budget.per_day", { n: fmt(m.tokens_24h) })}</span>
          <select
            aria-label={t("budget.class_of", { name: m.name })}
            value={m.budget_class ?? "normal"}
            onChange={(e) => setClass(m, e.target.value)}
            className="h-7 shrink-0 rounded border border-line bg-bg px-1 text-xs outline-none focus:border-accent"
          >
            {CLASSES.map((c) => (
              <option key={c} value={c}>
                {label("budget.class", c)}
              </option>
            ))}
          </select>
        </div>
      ))}
      {error && <p className="px-4 py-2 text-xs break-words text-red-400">{error}</p>}
    </Panel>
  );
}
