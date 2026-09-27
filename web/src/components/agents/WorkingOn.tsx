import { Link } from "react-router-dom";
import { t } from "../../i18n";

/**
 * What a member works on right now. `working_on` on the agent payload is the
 * one source; older payloads carry `current` (agents: {ref, title}; chat
 * members: {task_ref, since}), read the same way until every API sends it.
 */
export type WorkingOn = { task_ref: string | null; since: string | null; title?: string | null };

type Source = {
  working_on?: { task_ref?: string | null; since?: string | null; title?: string | null } | null;
  current?: { ref?: string | null; task_ref?: string | null; title?: string | null; since?: string | null } | null;
};

export function workingOn(x: Source | null | undefined): WorkingOn | null {
  if (!x) return null;
  // The payload carries working_on (even null = not working): that is the answer, no fallback.
  if ("working_on" in x) return x.working_on ? { task_ref: x.working_on.task_ref ?? null, since: x.working_on.since ?? null, title: x.working_on.title ?? null } : null;
  const c = x.current;
  if (!c) return null;
  return { task_ref: c.task_ref ?? c.ref ?? null, since: c.since ?? null, title: c.title ?? null };
}

function minutes(since: string | null) {
  if (!since) return null;
  return Math.max(0, Math.floor((Date.now() - new Date(since).getTime()) / 60000));
}

/** "pracuje na T-046 · 12 min", linking to the task; the same words in chat, Tým and the agent page. */
export function WorkingOnText({ w, withTitle = false, className = "" }: { w: WorkingOn; withTitle?: boolean; className?: string }) {
  const m = minutes(w.since);
  return (
    <span className={className} title={w.title ?? undefined}>
      {t("working.on")}{" "}
      {w.task_ref ? (
        <Link to={`/tasks?task=${w.task_ref}`} className="font-mono text-accent hover:underline">
          {w.task_ref}
        </Link>
      ) : (
        t("working.run")
      )}
      {withTitle && w.title ? ` ${w.title}` : ""}
      {m != null ? ` · ${m} min` : ""}
    </span>
  );
}

export function WorkingDot({ on }: { on: boolean }) {
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${on ? "sonar bg-accent" : "bg-ink-3"}`} aria-hidden />;
}
