import { Boxes, Orbit, User, Users } from "lucide-react";
import { label, t } from "../../i18n";
import type { AssigneeType, Task } from "../../tasksApi";

const CHIP: Record<AssigneeType, { cls: string; Icon: typeof User }> = {
  human: { cls: "border border-line text-ink", Icon: User },
  ai: { cls: "bg-accent/10 text-accent border border-transparent", Icon: Orbit },
  agent: { cls: "border border-dashed border-accent text-accent", Icon: Boxes },
  external: { cls: "border border-dashed border-ink-3 text-ink-2", Icon: Users },
};

export function AssigneeChip({ type, name }: { type: AssigneeType | null; name: string | null }) {
  if (!type) return <span className="text-xs text-ink-2">{t("who.unassigned")}</span>;
  const { cls, Icon } = CHIP[type];
  const text = type === "ai" ? t("who.ai") : type === "human" && name === "Owner" ? t("who.me") : (name ?? type);
  return (
    <span className={`inline-flex h-[22px] max-w-full min-w-0 items-center gap-1.5 rounded-[3px] px-1.5 text-xs whitespace-nowrap ${cls}`}>
      <Icon size={12} strokeWidth={1.6} className="shrink-0" />
      <span className="truncate">{text}</span>
    </span>
  );
}

export function Energy({ level }: { level: Task["energy"] }) {
  const n = level === "high" ? 3 : level === "low" ? 1 : 0;
  return (
    <span title={level ? t(`work.energy.${level}`) : t("work.energy.unset")} className="inline-flex items-end gap-0.5">
      {[0, 1, 2].map((i) => (
        <span key={i} className={`w-[3px] ${i < n ? "bg-ink" : "bg-line"}`} style={{ height: 4 + i * 3 }} />
      ))}
    </span>
  );
}

const STATE: Partial<Record<Task["status"], string>> = {
  working: "border-accent text-accent",
  review: "border-amber-400/70 text-amber-300",
  waiting: "border-line text-ink-2",
};

export function StatePill({ task }: { task: Task }) {
  const cls = STATE[task.status];
  if (!cls) return null;
  const text =
    task.status === "working" && task.progress != null ? t("work.state.working_pct", { n: task.progress }) : label("task", task.status);
  return <span className={`shrink-0 rounded-[3px] border px-1.5 py-px text-xs whitespace-nowrap ${cls}`}>{text}</span>;
}

export function fmtMinutes(min: number | null | undefined) {
  if (!min) return "—";
  if (min < 60) return t("work.fmt.min", { n: min });
  const h = Math.floor(min / 60);
  return min % 60 ? t("work.fmt.hm", { h, m: min % 60 }) : t("work.fmt.h", { h });
}
