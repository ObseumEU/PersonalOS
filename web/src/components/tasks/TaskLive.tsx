import { useEffect, useState } from "react";
import { LOCALE, label, t } from "../../i18n";
import { type TaskLive as Live, tasksApi } from "../../tasksApi";
import Markdown from "../Markdown";
import { EngineBadge } from "./AgentPicker";

const STATE_KEY: Record<string, string> = {
  working: "work.live.working",
  queued: "work.live.queued",
  blocked: "work.live.blocked",
};

const time = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";

/** Live status of a task: who has it, the current run with its engine/model, progress notes, and why it is stuck. */
export default function TaskLive({ taskRef, version, onChange }: { taskRef: string; version: string; onChange?: () => void }) {
  const [live, setLive] = useState<Live | null>(null);

  useEffect(() => {
    let alive = true;
    let last = "";
    const load = () =>
      tasksApi.live(taskRef).then(
        (l) => {
          if (!alive) return;
          setLive(l);
          const sig = `${l.status}|${l.progress}|${l.progress_note}|${l.assignee?.id}`;
          if (last && sig !== last) onChange?.();
          last = sig;
        },
        () => undefined,
      );
    load();
    const id = window.setInterval(load, 4000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [taskRef, version]);

  if (!live || !live.assignee || live.assignee.kind === "human" || live.status === "done") return null;
  const a = live.assignee;
  const run = live.run;
  const blocked = live.blocked.length > 0 && live.state === "blocked";
  const state = STATE_KEY[live.state] ? t(STATE_KEY[live.state]) : live.state;

  return (
    <div
      className={`flex min-w-0 flex-col gap-2 rounded border p-3 ${blocked ? "border-amber-400/60" : run ? "border-accent/60" : "border-line"}`}
      aria-live="polite"
    >
      <span className="flex min-w-0 flex-wrap items-center gap-2">
        <span className={`text-xs font-medium ${blocked ? "text-amber-300" : run ? "text-accent" : "text-ink-2"}`}>
          {t("work.live.state", { state })}
        </span>
        <span className="min-w-0 truncate text-[13px]">{a.name}</span>
        {a.role && <span className="text-xs text-ink-2">{a.role.replace(/_/g, " ")}</span>}
        <span className="ml-auto flex items-center gap-1.5">
          <EngineBadge label={run?.label ?? a.engine_label} />
          <span
            title={
              a.worker_online
                ? a.worker_waiting
                  ? t("work.live.worker_waiting")
                  : t("work.live.worker_online")
                : t("work.live.worker_offline")
            }
            className={`h-1.5 w-1.5 rounded-full ${a.worker_online ? "bg-emerald-400" : "bg-dim"}`}
          />
        </span>
      </span>

      {blocked &&
        live.blocked.map((b) => (
          <span key={b.code + b.text} className="text-xs leading-snug break-words text-amber-300">
            {b.text}
          </span>
        ))}

      {run ? (
        <span className="text-xs text-ink-2">
          {t("work.live.run", { id: run.id, start: time(run.started_at), beat: time(run.heartbeat_at) })}
        </span>
      ) : (
        live.runs[0] && (
          <span className="text-xs break-words text-ink-2">
            {t("work.live.last_run", { id: live.runs[0].id, status: label("runstatus", live.runs[0].status) })}
            {live.runs[0].detail ? ` · ${live.runs[0].detail}` : ""}
          </span>
        )
      )}

      {live.progress != null && live.status === "working" && (
        <div className="h-1.5 rounded-[1px] bg-line">
          <span className="block h-full bg-accent" style={{ width: `${live.progress}%` }} />
        </div>
      )}

      {live.notes.length > 0 && (
        <ol className="flex flex-col gap-1">
          {live.notes.slice(0, 5).map((n, i) => (
            <li key={i} className="flex gap-2 text-xs leading-snug">
              <span className="w-16 shrink-0 text-ink-2 tabular-nums">{time(n.at)}</span>
              <span className="min-w-0 flex-1 break-words">
                <Markdown
                  compact
                  className="md-muted text-xs!"
                  text={`${n.progress != null && n.progress > 0 ? `**${n.progress} %** · ` : ""}${n.note ?? ""}`}
                />
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
