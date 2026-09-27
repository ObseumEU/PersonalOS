import { Boxes, Orbit, User, Users } from "lucide-react";
import { t } from "../../i18n";
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

/**
 * One colour per status, everywhere a task's status shows (list, detail, agents, projects).
 * "you" is not a status: an open question or result that waits for the signed-in person.
 */
export type Tone = Task["status"] | "you";

const TONE: Record<Tone, string> = {
  inbox: "bg-slate-400/10 text-slate-300 ring-slate-400/30",
  next: "bg-indigo-400/10 text-indigo-200 ring-indigo-400/30",
  working: "bg-cyan-400/10 text-cyan-200 ring-cyan-400/40",
  review: "bg-amber-400/10 text-amber-200 ring-amber-400/40",
  waiting: "bg-violet-400/10 text-violet-200 ring-violet-400/30",
  someday: "bg-zinc-400/10 text-zinc-300 ring-zinc-400/25",
  done: "bg-emerald-400/10 text-emerald-200 ring-emerald-400/30",
  you: "bg-orange-400/15 text-orange-200 ring-orange-400/50",
};

const DOT: Record<Tone, string> = {
  inbox: "bg-slate-300",
  next: "bg-indigo-300",
  working: "bg-cyan-300",
  review: "bg-amber-300",
  waiting: "bg-violet-300",
  someday: "bg-zinc-400",
  done: "bg-emerald-300",
  you: "bg-orange-300",
};

export const statusText = (tone: Tone) => t(`tk.status.${tone}`);

/** A status as a coloured chip ("Probíhá 40 %", "Ke kontrole", "Čeká na tebe"). */
export function StatusChip({ tone, progress, size = "sm" }: { tone: Tone; progress?: number | null; size?: "sm" | "md" }) {
  const text = tone === "working" && progress ? `${statusText(tone)} ${progress} %` : statusText(tone);
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full whitespace-nowrap ring-1 ring-inset ${TONE[tone]} ${
        size === "md" ? "h-7 px-3 text-[13px] font-medium" : "h-[22px] px-2 text-xs"
      }`}
    >
      <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${DOT[tone]}`} />
      {text}
    </span>
  );
}

/** The tone of a task for the signed-in person: an open ask ticket for them reads "Čeká na tebe". */
export function toneOf(task: Pick<Task, "status" | "source" | "assignee_id">, meId?: number | null): Tone {
  if (task.source === "ask_owner" && task.status !== "done" && meId != null && task.assignee_id === meId) return "you";
  return task.status;
}

export function StatePill({ task }: { task: Task }) {
  return <StatusChip tone={task.status} progress={task.status === "working" ? task.progress : null} />;
}

const AVATAR: Record<AssigneeType, string> = {
  human: "bg-ink/10 text-ink",
  ai: "bg-violet-400/15 text-violet-200",
  agent: "bg-accent/15 text-accent",
  external: "border border-dashed border-ink-3 text-ink-2",
};

export function initials(name: string | null | undefined): string {
  const words = (name ?? "?").replace(/[^\p{L}\p{N} &]/gu, " ").split(/\s+/).filter((w) => w && w !== "&");
  if (!words.length) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

/** A round avatar with initials; the colour says person, assistant, agent or someone outside. */
export function Avatar({ type, name, size = 28 }: { type: AssigneeType | null; name: string | null; size?: number }) {
  const cls = type ? AVATAR[type] : "border border-dashed border-line text-ink-3";
  return (
    <span
      aria-hidden
      title={name ?? undefined}
      style={{ width: size, height: size, fontSize: Math.max(11, Math.round(size * 0.4)) }}
      className={`inline-grid shrink-0 place-items-center rounded-full font-medium ${cls}`}
    >
      {type ? initials(name) : "–"}
    </span>
  );
}

export function fmtMinutes(min: number | null | undefined) {
  if (!min) return "—";
  if (min < 60) return t("work.fmt.min", { n: min });
  const h = Math.floor(min / 60);
  return min % 60 ? t("work.fmt.hm", { h, m: min % 60 }) : t("work.fmt.h", { h });
}
