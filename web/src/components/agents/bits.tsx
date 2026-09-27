import type { Agent, AgentStatus, EngineView } from "../../agentsApi";
import { fmtDateTime, label, t } from "../../i18n";
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
  const tone = status === "approval" ? "text-amber-300" : status === "working" || status === "online" ? "text-accent" : "text-ink-2";
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs ${tone}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${COLOR[status]} ${status === "working" ? "sonar" : ""}`} />
      {label("status", status)}
    </span>
  );
}

export function ActorChip({ a }: { a: Pick<Agent, "kind" | "name" | "is_owner"> }) {
  const type = a.kind === "human" ? "human" : a.kind === "ai" ? "ai" : "agent";
  return <AssigneeChip type={type} name={a.is_owner ? t("who.owner") : a.name} />;
}

/** Engine and model: the running run's, else what the next run uses; amber on the fallback. */
export function EngineBadge({ view, engine, model }: { view?: EngineView | null; engine?: string | null; model?: string | null }) {
  if (view) {
    const shown = view.last_run?.running ? view.last_run : view.now;
    const fallback = "fallback" in shown && shown.fallback;
    const setting =
      view.setting === "auto" ? t(view.primary === "codex" ? "bits.auto_codex" : "bits.auto_claude") : label("engine", view.setting);
    const title = [
      t("bits.setting", { v: setting }),
      t(view.last_run?.running ? "bits.running_on" : "bits.next_on", { v: shown.label }),
      view.last_run && !view.last_run.running ? t("bits.last_run", { v: view.last_run.label, at: fmtDateTime(view.last_run.at) }) : null,
    ]
      .filter(Boolean)
      .join("\n");
    return (
      <span
        className={`rounded-[3px] border px-1.5 py-px font-mono text-xs ${fallback ? "border-amber-400/60 text-amber-300" : "border-accent/50 text-accent"}`}
        title={title}
      >
        {shown.label}
      </span>
    );
  }
  if (!engine) return null;
  const text =
    engine === "claude" ? `Claude${model ? ` · ${model.replace("claude-", "")}` : ""}` : engine === "codex" ? "Codex" : t("bits.auto");
  return (
    <span className="rounded-[3px] border border-accent/50 px-1.5 py-px font-mono text-xs text-accent" title={t("bits.runtime_title")}>
      {text}
    </span>
  );
}

export function Pill({ children, warn = false }: { children: string; warn?: boolean }) {
  return (
    <span className={`rounded-[3px] border px-1.5 py-px text-xs ${warn ? "border-amber-400/60 text-amber-300" : "border-line text-ink-2"}`}>
      {children}
    </span>
  );
}
