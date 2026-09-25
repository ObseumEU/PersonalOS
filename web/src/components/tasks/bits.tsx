import { Boxes, Orbit, User, Users } from "lucide-react";
import type { AssigneeType, Task } from "../../tasksApi";

const CHIP: Record<AssigneeType, { cls: string; Icon: typeof User }> = {
  human: { cls: "border border-line text-ink", Icon: User },
  ai: { cls: "bg-accent/10 text-accent border border-transparent", Icon: Orbit },
  agent: { cls: "border border-dashed border-accent text-accent", Icon: Boxes },
  external: { cls: "border border-dashed border-ink-3 text-ink-2", Icon: Users },
};

export function AssigneeChip({ type, name }: { type: AssigneeType | null; name: string | null }) {
  if (!type) return <span className="cap">unassigned</span>;
  const { cls, Icon } = CHIP[type];
  const label = type === "ai" ? "AI" : type === "human" && name === "Owner" ? "Me" : (name ?? type);
  return (
    <span className={`inline-flex h-[22px] items-center gap-1.5 rounded-[3px] px-1.5 font-mono text-[11px] whitespace-nowrap ${cls}`}>
      <Icon size={12} strokeWidth={1.6} />
      {label}
    </span>
  );
}

export function Energy({ level }: { level: Task["energy"] }) {
  const n = level === "high" ? 3 : level === "low" ? 1 : 0;
  return (
    <span title={level ? `${level} energy` : "energy not set"} className="inline-flex items-end gap-0.5">
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
    task.status === "working" && task.progress != null ? `working ${task.progress}%` : task.status === "review" ? "review" : task.status;
  return <span className={`cap rounded-[3px] border px-1.5 py-px text-[10px]! ${cls}`}>{text}</span>;
}

export function fmtMinutes(min: number | null | undefined) {
  if (!min) return "—";
  if (min < 60) return `${min}m`;
  return `${Math.floor(min / 60)}h${min % 60 ? `${min % 60}m` : ""}`;
}
