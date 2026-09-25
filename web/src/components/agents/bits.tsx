import type { Agent, AgentStatus } from "../../agentsApi";
import { AssigneeChip } from "../tasks/bits";

const COLOR: Record<AgentStatus, string> = {
  online: "bg-accent",
  working: "bg-accent",
  idle: "bg-ink-3",
  approval: "bg-amber-300",
  paused: "bg-ink-3",
  archived: "bg-dim",
};

export function StatusDot({ status }: { status: AgentStatus }) {
  const text = status === "approval" ? "needs approval" : status;
  return (
    <span className={`cap inline-flex items-center gap-1.5 ${status === "approval" ? "text-amber-300!" : status === "working" || status === "online" ? "text-accent!" : ""}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${COLOR[status]} ${status === "working" ? "sonar" : ""}`} />
      {text}
    </span>
  );
}

export function ActorChip({ a }: { a: Pick<Agent, "kind" | "name" | "is_owner"> }) {
  const type = a.kind === "human" ? "human" : a.kind === "ai" ? "ai" : "agent";
  return <AssigneeChip type={type} name={a.is_owner ? "Owner" : a.name} />;
}

export function EngineBadge({ engine, model }: { engine: string | null | undefined; model?: string | null }) {
  if (!engine) return null;
  const label = engine === "claude" ? `Claude${model ? ` · ${model.replace("claude-", "")}` : ""}` : engine === "codex" ? "Codex" : "auto";
  return (
    <span className="cap rounded-[3px] border border-accent/50 px-1.5 py-px text-[10px]! text-accent!" title="Runtime: which CLI and subscription this agent runs on">
      {label}
    </span>
  );
}

export function Pill({ children, warn = false }: { children: string; warn?: boolean }) {
  return (
    <span className={`cap rounded-[3px] border px-1.5 py-px text-[10px]! ${warn ? "border-amber-400/60 text-amber-300!" : "border-line"}`}>
      {children}
    </span>
  );
}
