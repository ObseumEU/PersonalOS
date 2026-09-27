import { Archive, Check, History, Pencil, RotateCcw, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../../api";
import { LOCALE, label, t } from "../../i18n";
import { type Actor, type Comment, NO_DESCRIPTION, PRIORITY_LABEL, type Task, type Version, tasksApi } from "../../tasksApi";
import { FeedbackForm } from "../Feedback";
import { markdownSnippet } from "../../markdownText";
import Markdown from "../Markdown";
import { confirmDialog, toast } from "../overlay";
import { Panel } from "../ui";
import AgentPicker from "./AgentPicker";
import { AssigneeChip, StatePill } from "./bits";
import TaskLive from "./TaskLive";

function Field({ label: text, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-xs text-ink-2">{text}</span>
      {children}
    </label>
  );
}

const input = "h-8 min-w-0 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

const STATUSES = ["inbox", "next", "working", "review", "waiting", "someday", "done"] as const;

const kindWord = (kind: string) => (kind === "ai" ? t("who.ai") : kind === "human" ? t("who.person") : kind === "agent" ? t("who.agent") : kind);

const stamp = (iso: string) => new Date(iso).toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" });

export function AssigneeSelect({ task, actors, onChange }: { task: Task; actors: Actor[]; onChange: (v: unknown) => void }) {
  const value =
    task.assignee_type === "external" ? "__external" : task.assignee_id ? String(task.assignee_id) : "";
  return (
    <select
      className={input}
      value={value}
      onChange={async (e) => {
        const v = e.target.value;
        if (v === "__external") {
          const name = await confirmDialog({
            title: t("work.assignee.external_ask"),
            confirm: t("work.assignee.assign"),
            reason: t("work.assignee.external_name"),
          });
          if (name) onChange({ type: "external", name });
        } else onChange(v ? { type: "human", id: Number(v) } : null);
      }}
    >
      <option value="">{t("who.unassigned")}</option>
      {actors.map((a) => (
        <option key={a.id} value={a.id}>
          {a.is_owner ? t("who.me") : a.name} · {kindWord(a.kind)}
        </option>
      ))}
      <option value="__external">{task.assignee_type === "external" ? task.assignee_name : t("work.assignee.external_option")}</option>
    </select>
  );
}

/** The task's description: what it is for, where it came from, what done looks like. */
export function Description({ task, onSave }: { task: Task; onSave: (notes: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(task.notes ?? "");
  useEffect(() => {
    setEditing(false);
    setDraft(task.notes ?? "");
  }, [task.ref, task.updated_at]);
  const empty = !(task.notes ?? "").trim();

  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <span className="flex items-baseline gap-2">
        <span className="text-xs text-ink-2">{t("work.desc.label")}</span>
        {!empty && task.description_generated ? (
          <span className="text-xs text-ink-2" title={t("work.desc.generated_hint")}>
            {t("work.desc.generated")}
          </span>
        ) : null}
        {!editing && (
          <button
            type="button"
            className="ml-auto inline-flex items-center gap-1 text-xs text-accent hover:underline"
            onClick={() => setEditing(true)}
          >
            <Pencil size={11} /> {empty ? t("work.desc.add") : t("act.edit")}
          </button>
        )}
      </span>
      {editing ? (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            onSave(draft);
            setEditing(false);
          }}
        >
          <textarea
            autoFocus
            rows={6}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={t("work.desc.placeholder")}
            aria-label={t("work.desc.label")}
            className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
          />
          <div className="flex gap-2">
            <button type="submit" className="btn-accent">
              <Check size={14} /> {t("act.save")}
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setDraft(task.notes ?? "");
                setEditing(false);
              }}
            >
              {t("act.cancel")}
            </button>
          </div>
        </form>
      ) : empty ? (
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="rounded border border-dashed border-line p-3 text-left text-[13px] text-ink-2 italic hover:border-accent"
        >
          {t("work.desc.empty_hint", { none: NO_DESCRIPTION })}
        </button>
      ) : (
        <div className="min-w-0 rounded border border-line px-4 py-3 break-words">
          <Markdown text={task.notes} />
        </div>
      )}
    </div>
  );
}

const KIND_KEY: Record<Comment["kind"], string> = {
  comment: "",
  return: "work.kind.return",
  review: "work.kind.review",
  handoff: "work.kind.handoff",
  progress: "work.kind.progress",
  system: "work.kind.system",
};

/** The task's activity: comments, returns, reviews, handoffs and progress, oldest first. */
function Activity({ taskRef, version }: { taskRef: string; version: string }) {
  const [items, setItems] = useState<Comment[] | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const load = () => tasksApi.comments(taskRef).then(setItems, (e) => setError(e.message));
  useEffect(() => {
    load();
  }, [taskRef, version]);
  const send = () => {
    if (!draft.trim()) return;
    tasksApi.comment(taskRef, draft.trim()).then(
      () => {
        setDraft("");
        load();
      },
      (e) => setError(e.message),
    );
  };
  return (
    <div className="flex min-w-0 flex-col">
      <span className="flex flex-wrap items-baseline gap-x-2 pb-1.5">
        <span className="text-[13px] font-medium">{t("work.activity.title")}</span>
        <span className="text-xs text-ink-2">{t("work.activity.hint")}</span>
      </span>
      {items?.length === 0 && <span className="border-t border-line py-2 text-xs text-ink-2">{t("work.activity.empty")}</span>}
      {items?.map((c) => (
        <div key={c.id} className="flex min-w-0 flex-col gap-0.5 border-t border-line py-2 break-words">
          <span className="text-xs text-ink-2">
            {c.author_name ?? t("work.activity.system")}
            {KIND_KEY[c.kind] ? ` · ${t(KIND_KEY[c.kind])}` : ""} · {stamp(c.created_at)}
          </span>
          <Markdown text={c.body} compact className={c.kind === "comment" ? "" : "md-muted"} />
        </div>
      ))}
      <div className="flex flex-col gap-1.5 border-t border-line pt-2">
        <textarea
          rows={2}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
          }}
          placeholder={t("work.activity.placeholder")}
          aria-label={t("work.activity.aria")}
          className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
        />
        <div className="flex items-center gap-2">
          <button type="button" className="btn" disabled={!draft.trim()} onClick={send}>
            {t("work.activity.send")}
          </button>
          {error && <span className="min-w-0 text-xs break-words text-red-400">{error}</span>}
        </div>
      </div>
    </div>
  );
}

/** Accept, or return with what should change (the note goes to the activity). */
function ReviewBox({ onAccept, onReturn }: { onAccept: () => void; onReturn: (comment: string) => void }) {
  const [returning, setReturning] = useState(false);
  const [comment, setComment] = useState("");
  return (
    <div className="flex flex-col gap-2 rounded border border-amber-400/60 p-3">
      <span className="text-xs font-medium text-amber-300">{t("work.review.waits")}</span>
      {returning ? (
        <>
          <textarea
            autoFocus
            rows={3}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder={t("work.review.placeholder")}
            aria-label={t("work.review.placeholder")}
            className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
          />
          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn-accent" disabled={!comment.trim()} onClick={() => onReturn(comment.trim())}>
              {t("work.review.return_with")}
            </button>
            <button type="button" className="btn" onClick={() => setReturning(false)}>
              {t("act.cancel")}
            </button>
          </div>
        </>
      ) : (
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn-accent" onClick={onAccept}>
            <Check size={14} /> {t("work.review.accept")}
          </button>
          <button type="button" className="btn" onClick={() => setReturning(true)}>
            {t("work.review.return")}
          </button>
        </div>
      )}
    </div>
  );
}

/** A private task: who else may see it (a member, or everyone in a project: "project:<slug>"). */
function ShareBox({ taskId }: { taskId: number }) {
  const [who, setWho] = useState<{ id: number; name: string }[]>([]);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const load = () => api<{ id: number; name: string }[]>(`/api/share/task/${taskId}`).then(setWho, () => setWho([]));
  useEffect(() => {
    load();
  }, [taskId]);
  const share = () =>
    api<{ id: number; name: string }[]>("/api/share", { method: "POST", body: JSON.stringify({ entity: "task", id: taskId, with: value.trim() }) }).then(
      (r) => {
        setWho(r);
        setValue("");
        setError(null);
      },
      (e) => setError(e.message),
    );
  return (
    <div className="flex flex-col gap-1.5 rounded border border-line p-3">
      <span className="text-xs break-words text-ink-2">
        {t("work.share.private", { who: who.length ? who.map((w) => w.name).join(", ") : t("work.share.nobody") })}
      </span>
      <div className="flex gap-2">
        <input
          className={`${input} flex-1`}
          placeholder={t("work.share.placeholder")}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label={t("work.share.aria")}
        />
        <button type="button" className="btn" disabled={!value.trim()} onClick={share}>
          {t("work.share.button")}
        </button>
      </div>
      {error && <span className="text-xs break-words text-red-400">{error}</span>}
    </div>
  );
}

export default function TaskDetail({
  taskRef,
  actors,
  onChanged,
  onClose,
}: {
  taskRef: string;
  actors: Actor[];
  onChanged: () => void;
  onClose: () => void;
}) {
  const [task, setTask] = useState<Task | null>(null);
  const [history, setHistory] = useState<Version[] | null>(null);
  const [step, setStep] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = () => tasksApi.get(taskRef).then(setTask, (e) => setError(e.message));
  useEffect(() => {
    setHistory(null);
    load();
  }, [taskRef]);

  async function run(p: Promise<unknown>): Promise<boolean> {
    setError(null);
    try {
      await p;
      await load();
      if (history) setHistory(await tasksApi.history(taskRef));
      onChanged();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    }
  }
  const save = (changes: Record<string, unknown>) => run(tasksApi.update(taskRef, changes));

  if (!task)
    return (
      <Panel title={t("work.detail.title")} className="w-full">
        {error && <p className="p-4 text-xs break-words text-red-400">{error}</p>}
      </Panel>
    );
  const agentWork = task.assignee_type === "ai" || task.assignee_type === "agent";
  // What an agent handed in (complete_task's note) reads as a result, not a status line.
  const showResult = !!task.progress_note?.trim() && (task.status === "review" || task.status === "done");

  return (
    <Panel
      title={t("work.detail.title")}
      className="w-full min-w-0"
      right={
        <button type="button" onClick={onClose} aria-label={t("work.detail.close")} className="text-ink-2 hover:text-ink">
          <X size={14} />
        </button>
      }
      bodyClassName="overflow-y-auto"
    >
      <div className="flex min-w-0 flex-col gap-4 p-4">
        {task.parent && (
          <span className="text-xs break-words text-ink-2">{t("work.detail.step_of", { ref: task.parent.ref, title: task.parent.title })}</span>
        )}
        <input
          key={task.updated_at}
          defaultValue={task.title}
          onBlur={(e) => e.target.value !== task.title && save({ title: e.target.value })}
          className="min-w-0 bg-transparent text-xl font-light tracking-[-0.01em] outline-none focus:border-b focus:border-accent"
          aria-label={t("work.detail.title_aria")}
        />
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <AgentPicker task={task} align="left" onReassigned={() => run(Promise.resolve())} />
          <StatePill task={task} />
          {task.progress_note && !showResult && (
            <span className="min-w-0 text-xs break-words text-ink-2">{markdownSnippet(task.progress_note, 160)}</span>
          )}
        </div>

        {showResult && (
          <div className="flex min-w-0 flex-col gap-1.5">
            <span className="text-xs text-ink-2">{t("work.detail.result")}</span>
            <div className="min-w-0 rounded border border-line px-4 py-3 break-words">
              <Markdown text={task.progress_note!} />
            </div>
          </div>
        )}

        <TaskLive taskRef={task.ref} version={task.updated_at} onChange={() => run(Promise.resolve())} />

        <Description task={task} onSave={(notes) => notes !== task.notes && save({ notes })} />

        {task.status === "review" && task.can_review !== false && (
          <ReviewBox
            key={task.updated_at}
            onAccept={() => run(tasksApi.review(task.ref, true))}
            onReturn={(c) => run(tasksApi.review(task.ref, false, c))}
          />
        )}

        <div className="grid grid-cols-1 gap-3 min-[360px]:grid-cols-2">
          <Field label={t("work.detail.status")}>
            <select className={input} value={task.status} onChange={(e) => save({ status: e.target.value })}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {label("task", s)}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("work.detail.assignee")}>
            <AssigneeSelect task={task} actors={actors} onChange={(v) => save({ assignee: v })} />
          </Field>
          <Field label={t("work.detail.reviewer")}>
            <select
              className={input}
              value={task.reviewer_id ?? ""}
              onChange={(e) => save({ reviewer: e.target.value ? Number(e.target.value) : null })}
              title={t("work.detail.reviewer_hint")}
            >
              <option value="">{t("work.detail.reviewer_default", { name: task.reviewer_name ?? t("who.owner") })}</option>
              {actors
                .filter((a) => a.id !== task.assignee_id)
                .map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.is_owner ? t("who.me") : a.name}
                  </option>
                ))}
            </select>
          </Field>
          <Field label={t("work.detail.do_date")}>
            <input type="date" className={input} value={task.do_date ?? ""} onChange={(e) => save({ do_date: e.target.value || null })} />
          </Field>
          <Field label={t("work.detail.deadline")}>
            <input type="date" className={input} value={task.deadline ?? ""} onChange={(e) => save({ deadline: e.target.value || null })} />
          </Field>
          <Field label={t("priority.label")}>
            <select className={input} value={task.priority ?? ""} onChange={(e) => save({ priority: e.target.value ? Number(e.target.value) : null })}>
              <option value="">{t("work.priority.unset")}</option>
              <option value="1">P1 · {PRIORITY_LABEL[1]}</option>
              <option value="2">P2 · {PRIORITY_LABEL[2]}</option>
              <option value="3">P3 · {PRIORITY_LABEL[3]}</option>
            </select>
          </Field>
          <Field label={t("work.detail.value")}>
            <select
              className={input}
              value={task.value_kind ?? ""}
              title={t("work.value.help")}
              onChange={(e) => save({ value_kind: e.target.value || null })}
            >
              <option value="">{t("work.value.auto", { v: t(`work.value.${task.value_kind_effective ?? "platform"}`) })}</option>
              <option value="business">{t("work.value.business")}</option>
              <option value="platform">{t("work.value.platform")}</option>
              <option value="demo">{t("work.value.demo_long")}</option>
            </select>
          </Field>
          <Field label={t("work.detail.energy")}>
            <select className={input} value={task.energy ?? ""} onChange={(e) => save({ energy: e.target.value || null })}>
              <option value="">{t("work.energy.opt.unset")}</option>
              <option value="high">{t("work.energy.opt.high")}</option>
              <option value="low">{t("work.energy.opt.low")}</option>
            </select>
          </Field>
          <Field label={t("work.detail.estimate")}>
            <input
              key={`e${task.updated_at}`}
              type="number"
              min={0}
              className={input}
              defaultValue={task.estimate_min ?? ""}
              onBlur={(e) => save({ estimate_min: e.target.value ? Number(e.target.value) : null })}
            />
          </Field>
          <Field label={t("work.detail.topic")}>
            <input
              key={`t${task.updated_at}`}
              className={input}
              defaultValue={task.topic ?? ""}
              onBlur={(e) => (e.target.value || null) !== task.topic && save({ topic: e.target.value || null })}
            />
          </Field>
          <Field label={t("work.detail.visibility")}>
            <select className={input} value={task.visibility} onChange={(e) => save({ visibility: e.target.value })}>
              <option value="team">{t("work.visibility.team")}</option>
              <option value="private">{t("work.visibility.private")}</option>
              <option value="public">{t("work.visibility.public")}</option>
            </select>
          </Field>
          {task.status === "waiting" && (
            <Field label={t("work.detail.follow_up")}>
              <input type="date" className={input} value={task.follow_up ?? ""} onChange={(e) => save({ follow_up: e.target.value || null })} />
            </Field>
          )}
        </div>

        <Field label={t("work.detail.dod")}>
          <textarea
            key={`d${task.updated_at}`}
            rows={2}
            defaultValue={task.definition_of_done ?? ""}
            onBlur={(e) => (e.target.value || null) !== task.definition_of_done && save({ definition_of_done: e.target.value || null })}
            className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
          />
        </Field>

        {!task.parent_id && (
          <div className="flex min-w-0 flex-col">
            <span className="flex flex-wrap items-baseline gap-x-2 pb-1.5">
              <span className="text-[13px] font-medium">{t("work.steps.title")}</span>
              <span className="text-xs text-ink-2">
                {t("work.steps.summary", {
                  done: task.steps?.filter((s) => s.status === "done").length ?? 0,
                  total: task.steps?.length ?? 0,
                })}
              </span>
            </span>
            {task.steps?.map((s) => (
              <div key={s.id} className="flex min-w-0 items-center gap-2.5 border-t border-line py-2">
                <input
                  type="checkbox"
                  aria-label={t("work.row.complete", { title: s.title })}
                  checked={s.status === "done"}
                  onChange={() => run(s.status === "done" ? tasksApi.update(s.ref, { status: "next" }) : tasksApi.complete(s.ref))}
                  className="h-[15px] w-[15px] shrink-0 accent-accent"
                />
                <span className={`min-w-0 flex-1 truncate text-[13px] ${s.status === "done" ? "text-ink-3 line-through" : ""}`}>{s.title}</span>
                <StatePill task={s} />
                <AssigneeChip type={s.assignee_type} name={s.assignee_name} />
              </div>
            ))}
            <form
              className="flex gap-2 border-t border-line pt-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (!step.trim()) return;
                const m = step.match(/@(\S+)/);
                run(tasksApi.addStep(task.ref, step.replace(/@\S+/, "").trim(), m?.[1]));
                setStep("");
              }}
            >
              <input
                value={step}
                onChange={(e) => setStep(e.target.value)}
                placeholder={t("work.steps.placeholder")}
                className={`${input} flex-1`}
                aria-label={t("work.steps.aria")}
              />
            </form>
          </div>
        )}

        {task.visibility === "private" && <ShareBox taskId={task.id} />}

        <Activity taskRef={task.ref} version={task.updated_at} />

        {task.assignee_id && task.assignee_type !== "external" && (
          <details className="border-t border-line pt-2">
            <summary className="cursor-pointer text-xs text-ink-2">{t("work.detail.feedback", { name: task.assignee_name })}</summary>
            <div className="pt-2">
              <FeedbackForm to={task.assignee_id} taskRef={task.ref} />
            </div>
          </details>
        )}

        <div className="flex flex-wrap gap-2 border-t border-line pt-3">
          {task.status !== "done" && (
            <button type="button" className="btn" onClick={() => run(tasksApi.complete(task.ref))}>
              <Check size={14} /> {t("work.detail.complete")}
            </button>
          )}
          {task.status !== "done" && (
            <AgentPicker task={task} trigger="button" align="left" onReassigned={() => run(Promise.resolve())} />
          )}
          {agentWork && task.status !== "done" && (
            <button
              type="button"
              className="btn"
              title={t("work.detail.intervene_hint")}
              onClick={async () => {
                const n = await confirmDialog({
                  title: t("work.detail.intervene_title"),
                  body: t("work.detail.intervene_hint"),
                  confirm: t("work.detail.intervene_confirm"),
                  reason: t("work.detail.intervene_ask"),
                });
                if (n !== null) run(tasksApi.intervene(task.ref, n));
              }}
            >
              {t("work.detail.intervene")}
            </button>
          )}
          <button type="button" className="btn" onClick={async () => setHistory(history ? null : await tasksApi.history(task.ref))}>
            <History size={14} /> {t("work.detail.history")}
          </button>
          <button
            type="button"
            className="btn ml-auto"
            onClick={() =>
              run(tasksApi.archive(task.ref)).then((ok) => {
                if (ok) toast(t("work.detail.archived"));
                onClose();
              })
            }
          >
            <Archive size={14} /> {t("act.archive")}
          </button>
        </div>

        {history && (
          <ol className="flex min-w-0 flex-col">
            {[...history].reverse().map((h) => (
              <li key={h.version} className="flex min-w-0 items-center gap-2 border-t border-line py-1.5">
                <span className="w-7 shrink-0 font-mono text-xs text-ink-2">v{h.version}</span>
                <span className="shrink-0 text-xs">{h.action}</span>
                <span className="min-w-0 truncate text-xs text-ink-2">
                  {h.actor_name} · {stamp(h.at)}
                  {h.run_id ? ` · ${t("work.detail.run", { id: h.run_id })}` : ""}
                </span>
                {h.version < history.length && (
                  <button
                    type="button"
                    title={t("work.detail.restore", { n: h.version })}
                    aria-label={t("work.detail.restore", { n: h.version })}
                    className="ml-auto shrink-0 text-ink-2 hover:text-accent"
                    onClick={() => run(tasksApi.restore(task.ref, h.version))}
                  >
                    <RotateCcw size={13} />
                  </button>
                )}
              </li>
            ))}
          </ol>
        )}
        {error && <p className="text-xs break-words text-red-400">{error}</p>}
      </div>
    </Panel>
  );
}
