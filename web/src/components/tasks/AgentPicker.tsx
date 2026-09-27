import { ArrowRightLeft, Boxes, ChevronDown, Orbit, User } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { type Candidate, type ReassignResult, type Task, tasksApi } from "../../tasksApi";
import { t } from "../../i18n";
import { confirmDialog } from "../overlay";
import { AssigneeChip } from "./bits";

const KIND_ICON = { human: User, ai: Orbit, agent: Boxes } as const;

/** Engine/model badge, e.g. "Claude · Opus" or "Codex". */
export function EngineBadge({ label }: { label: string | null | undefined }) {
  if (!label) return null;
  return (
    <span className="inline-flex h-[18px] items-center rounded-[3px] border border-line px-1 font-mono text-xs whitespace-nowrap text-ink-2">
      {label}
    </span>
  );
}

function role(c: Candidate) {
  if (c.is_owner) return t("work.picker.owner");
  if (c.role) return c.role.replace(/_/g, " ");
  return c.kind === "ai" ? t("work.picker.assistant") : c.kind === "human" ? t("who.person") : t("who.agent");
}

/**
 * Reassign a task to another member. Shows each member's role and engine/model,
 * and why someone cannot take the task now (permissions, private task, pause,
 * kill switch, budget). Used in the task list, the task detail and the topic view;
 * all go through POST /api/tasks/{ref}/reassign, so the new agent is told and
 * its worker starts right away.
 */
export default function AgentPicker({
  task,
  onReassigned,
  trigger = "chip",
  align = "right",
}: {
  task: Pick<Task, "ref" | "assignee_type" | "assignee_name" | "assignee_id" | "status">;
  onReassigned: (r: ReassignResult) => void;
  /** "chip": the current assignee is the button; "button": a "Reassign" button. */
  trigger?: "chip" | "button";
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<Candidate[] | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top?: number; bottom?: number; left: number } | null>(null);

  // Fixed position, so a scrolling list or panel never clips the picker.
  function place() {
    const r = box.current?.getBoundingClientRect();
    if (!r) return;
    const width = Math.min(320, window.innerWidth - 32);
    const left = Math.max(16, Math.min(align === "right" ? r.right - width : r.left, window.innerWidth - width - 16));
    const below = window.innerHeight - r.bottom;
    setPos(below < 300 && r.top > below ? { bottom: window.innerHeight - r.top + 4, left } : { top: r.bottom + 4, left });
  }

  useEffect(() => {
    if (!open) return;
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setOptions(null);
    setError(null);
    tasksApi.reassignOptions(task.ref).then(setOptions, (e) => setError(e.message));
    const close = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open, task.ref]);

  async function pick(c: Candidate) {
    if (c.current) return;
    const hard = c.blocked.filter((b) => !b.soft);
    if (hard.length) return;
    let force = false;
    if (c.blocked.length) {
      const why = c.blocked.map((b) => b.text).join("\n");
      const ok = await confirmDialog({
        title: t("work.picker.blocked_title", { name: c.name }),
        body: t("work.picker.blocked_body", { why, name: c.name }),
        confirm: t("work.picker.blocked_confirm"),
      });
      if (ok === null) return;
      force = true;
    }
    setBusy(c.id);
    setError(null);
    try {
      const r = await tasksApi.reassign(task.ref, c.id, note.trim() || undefined, force);
      setOpen(false);
      setNote("");
      onReassigned(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const done = task.status === "done";
  let button: ReactNode;
  if (trigger === "chip") {
    button = (
      <button
        type="button"
        disabled={done}
        title={done ? t("work.picker.done") : t("work.picker.reassign")}
        aria-label={t("work.picker.reassign_ref", { ref: task.ref })}
        aria-expanded={open}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((o) => !o);
        }}
        className="group inline-flex items-center gap-0.5 rounded-[3px] hover:bg-raised disabled:cursor-default"
      >
        <AssigneeChip type={task.assignee_type} name={task.assignee_name} />
        {!done && <ChevronDown size={12} className="text-ink-3 group-hover:text-accent" />}
      </button>
    );
  } else {
    button = (
      <button
        type="button"
        className="btn"
        disabled={done}
        aria-expanded={open}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((o) => !o);
        }}
      >
        <ArrowRightLeft size={14} /> {t("work.picker.reassign")}
      </button>
    );
  }

  return (
    <div ref={box} className="relative inline-flex" onClick={(e) => e.stopPropagation()}>
      {button}
      {open && pos && (
        <div
          role="dialog"
          aria-label={t("work.picker.reassign_ref", { ref: task.ref })}
          style={{ position: "fixed", ...pos }}
          className="panel z-50 flex max-h-[min(420px,70vh)] w-[320px] max-w-[calc(100vw-32px)] flex-col shadow-lg"
        >
          <div className="flex items-baseline gap-2 border-b border-line px-3 py-2">
            <span className="shrink-0 text-xs font-medium">{t("work.picker.reassign_ref", { ref: task.ref })}</span>
            <span className="ml-auto min-w-0 truncate text-xs text-ink-2">{t("work.picker.hint")}</span>
          </div>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={t("work.picker.note")}
            aria-label={t("work.picker.note_aria")}
            className="mx-3 my-2 h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent"
          />
          <ul className="min-h-0 overflow-y-auto">
            {!options && !error && <li className="px-3 py-2 text-xs text-ink-2">{t("act.loading")}</li>}
            {options?.map((c) => {
              const Icon = KIND_ICON[c.kind] ?? Boxes;
              const hard = c.blocked.some((b) => !b.soft);
              return (
                <li key={c.id}>
                  <button
                    type="button"
                    disabled={c.current || hard || busy !== null}
                    onClick={() => pick(c)}
                    title={c.blocked.map((b) => b.text).join("\n") || undefined}
                    className={`flex w-full flex-col gap-0.5 border-t border-line px-3 py-2 text-left ${
                      c.current ? "bg-raised" : hard ? "opacity-60" : "hover:bg-raised"
                    }`}
                  >
                    <span className="flex w-full items-center gap-2">
                      <Icon size={13} strokeWidth={1.6} className={c.kind === "human" ? "text-ink-2" : "text-accent"} />
                      <span className="truncate text-[13px]">{c.is_owner ? t("work.picker.me", { name: c.name }) : c.name}</span>
                      <span className="shrink-0 text-xs text-ink-2">{role(c)}</span>
                      <span className="ml-auto flex shrink-0 items-center gap-1.5">
                        <EngineBadge label={c.engine_label} />
                        {c.worker_online != null && (
                          <span
                            title={c.worker_online ? t("work.live.worker_online") : t("work.live.worker_offline")}
                            className={`h-1.5 w-1.5 rounded-full ${c.worker_online ? "bg-emerald-400" : "bg-dim"}`}
                          />
                        )}
                      </span>
                    </span>
                    {c.current && <span className="text-xs text-ink-2">{t("work.picker.current")}</span>}
                    {busy === c.id && <span className="text-xs text-accent">{t("work.picker.busy")}</span>}
                    {c.blocked.map((b) => (
                      <span key={b.code} className={`text-xs leading-snug ${b.soft ? "text-amber-300" : "text-red-400"}`}>
                        {b.text}
                      </span>
                    ))}
                  </button>
                </li>
              );
            })}
          </ul>
          {error && <p className="border-t border-line px-3 py-2 text-xs break-words text-red-400">{error}</p>}
        </div>
      )}
    </div>
  );
}
