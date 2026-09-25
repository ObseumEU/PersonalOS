import { Archive, Check, History, Pencil, RotateCcw, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { type Actor, type Comment, NO_DESCRIPTION, type Task, type Version, tasksApi } from "../../tasksApi";
import Markdown from "../Markdown";
import { Panel } from "../ui";
import AgentPicker from "./AgentPicker";
import { AssigneeChip, StatePill } from "./bits";
import TaskLive from "./TaskLive";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="cap">{label}</span>
      {children}
    </label>
  );
}

const input = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

export function AssigneeSelect({ task, actors, onChange }: { task: Task; actors: Actor[]; onChange: (v: unknown) => void }) {
  const value =
    task.assignee_type === "external" ? "__external" : task.assignee_id ? String(task.assignee_id) : "";
  return (
    <select
      className={input}
      value={value}
      onChange={(e) => {
        const v = e.target.value;
        if (v === "__external") {
          const name = window.prompt("Who outside PersonalOS is doing this?");
          if (name) onChange({ type: "external", name });
        } else onChange(v ? { type: "human", id: Number(v) } : null);
      }}
    >
      <option value="">Unassigned</option>
      {actors.map((a) => (
        <option key={a.id} value={a.id}>
          {a.is_owner ? "Me" : a.name} · {a.kind === "ai" ? "AI" : a.kind}
        </option>
      ))}
      <option value="__external">{task.assignee_type === "external" ? task.assignee_name : "Someone outside…"}</option>
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
    <div className="flex flex-col gap-1.5">
      <span className="flex items-baseline gap-2">
        <span className="cap">DESCRIPTION</span>
        {!empty && task.description_generated ? (
          <span className="cap" title="PersonalOS wrote it from the task's fields; edit it to add the real context">
            · generated
          </span>
        ) : null}
        {!editing && (
          <button
            type="button"
            className="cap ml-auto inline-flex items-center gap-1 text-accent! hover:underline"
            onClick={() => setEditing(true)}
          >
            <Pencil size={11} /> {empty ? "Add description" : "Edit"}
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
            placeholder="What is this task for, where did it come from, and what does done look like?"
            aria-label="Description"
            className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
          />
          <div className="flex gap-2">
            <button type="submit" className="btn-accent">
              <Check size={14} /> Save
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setDraft(task.notes ?? "");
                setEditing(false);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : empty ? (
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="rounded border border-dashed border-line p-3 text-left text-[13px] text-ink-3 italic hover:border-accent"
        >
          {NO_DESCRIPTION} · add what the task is for and what done looks like
        </button>
      ) : (
        <div className="rounded border border-line p-3 text-[13px] break-words">
          <Markdown text={task.notes} />
        </div>
      )}
    </div>
  );
}

const KIND_LABEL: Record<Comment["kind"], string> = {
  comment: "",
  return: "returned",
  review: "review",
  handoff: "handoff",
  progress: "progress",
  system: "system",
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
    <div className="flex flex-col">
      <span className="flex items-baseline gap-2 pb-1.5">
        <span className="text-[13px] font-medium">Activity</span>
        <span className="cap">comments, returns, reviews, handoffs · @name notifies</span>
      </span>
      {items?.length === 0 && <span className="cap border-t border-line py-2">nothing yet</span>}
      {items?.map((c) => (
        <div key={c.id} className="flex flex-col gap-0.5 border-t border-line py-2">
          <span className="cap">
            {c.author_name ?? "system"}
            {KIND_LABEL[c.kind] ? ` · ${KIND_LABEL[c.kind]}` : ""} ·{" "}
            {new Date(c.created_at).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" })}
          </span>
          <span className={`text-[13px] whitespace-pre-wrap ${c.kind === "comment" ? "" : "text-ink-2"}`}>{c.body}</span>
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
          placeholder="Comment… (Ctrl+Enter)"
          aria-label="Comment"
          className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
        />
        <div className="flex items-center gap-2">
          <button type="button" className="btn" disabled={!draft.trim()} onClick={send}>
            Comment
          </button>
          {error && <span className="cap text-red-400!">{error}</span>}
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
      <span className="cap text-amber-300!">RESULT WAITS FOR REVIEW</span>
      {returning ? (
        <>
          <textarea
            autoFocus
            rows={3}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="What should change?"
            aria-label="What should change"
            className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
          />
          <div className="flex gap-2">
            <button type="button" className="btn-accent" disabled={!comment.trim()} onClick={() => onReturn(comment.trim())}>
              Return with this note
            </button>
            <button type="button" className="btn" onClick={() => setReturning(false)}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <div className="flex gap-2">
          <button type="button" className="btn-accent" onClick={onAccept}>
            <Check size={14} /> Accept
          </button>
          <button type="button" className="btn" onClick={() => setReturning(true)}>
            Return…
          </button>
        </div>
      )}
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

  async function run(p: Promise<unknown>) {
    setError(null);
    try {
      await p;
      await load();
      if (history) setHistory(await tasksApi.history(taskRef));
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const save = (changes: Record<string, unknown>) => run(tasksApi.update(taskRef, changes));

  if (!task) return <Panel title="Detail" className="w-full">{error && <p className="cap p-4 text-red-400!">{error}</p>}</Panel>;
  const agentWork = task.assignee_type === "ai" || task.assignee_type === "agent";

  return (
    <Panel
      fig={task.ref}
      title="Detail"
      className="w-full"
      right={
        <button type="button" onClick={onClose} aria-label="Close detail" className="text-ink-3 hover:text-ink">
          <X size={14} />
        </button>
      }
      bodyClassName="overflow-y-auto"
    >
      <div className="flex flex-col gap-4 p-4">
        {task.parent && (
          <span className="cap">
            step of {task.parent.ref} · {task.parent.title}
          </span>
        )}
        <input
          key={task.updated_at}
          defaultValue={task.title}
          onBlur={(e) => e.target.value !== task.title && save({ title: e.target.value })}
          className="bg-transparent text-xl font-light tracking-[-0.01em] outline-none focus:border-b focus:border-accent"
          aria-label="Title"
        />
        <div className="flex flex-wrap items-center gap-2">
          <AgentPicker task={task} align="left" onReassigned={() => run(Promise.resolve())} />
          <StatePill task={task} />
          {task.progress_note && <span className="cap">{task.progress_note}</span>}
        </div>

        <TaskLive taskRef={task.ref} version={task.updated_at} onChange={() => run(Promise.resolve())} />

        <Description task={task} onSave={(notes) => notes !== task.notes && save({ notes })} />

        {task.status === "review" && task.can_review !== false && (
          <ReviewBox
            key={task.updated_at}
            onAccept={() => run(tasksApi.review(task.ref, true))}
            onReturn={(c) => run(tasksApi.review(task.ref, false, c))}
          />
        )}

        <div className="grid grid-cols-2 gap-3">
          <Field label="STATUS">
            <select className={input} value={task.status} onChange={(e) => save({ status: e.target.value })}>
              {["inbox", "next", "working", "review", "waiting", "someday", "done"].map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </Field>
          <Field label="ASSIGNEE">
            <AssigneeSelect task={task} actors={actors} onChange={(v) => save({ assignee: v })} />
          </Field>
          <Field label="REVIEWER">
            <select
              className={input}
              value={task.reviewer_id ?? ""}
              onChange={(e) => save({ reviewer: e.target.value ? Number(e.target.value) : null })}
              title="Who accepts or returns the result"
            >
              <option value="">default · {task.reviewer_name ?? "owner"}</option>
              {actors
                .filter((a) => a.id !== task.assignee_id)
                .map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.is_owner ? "Me" : a.name}
                  </option>
                ))}
            </select>
          </Field>
          <Field label="DO DATE">
            <input type="date" className={input} value={task.do_date ?? ""} onChange={(e) => save({ do_date: e.target.value || null })} />
          </Field>
          <Field label="DEADLINE">
            <input type="date" className={input} value={task.deadline ?? ""} onChange={(e) => save({ deadline: e.target.value || null })} />
          </Field>
          <Field label="PRIORITY">
            <select className={input} value={task.priority ?? ""} onChange={(e) => save({ priority: e.target.value ? Number(e.target.value) : null })}>
              <option value="">none</option>
              <option value="1">P1 · Must</option>
              <option value="2">P2 · Should</option>
              <option value="3">P3 · Could</option>
            </select>
          </Field>
          <Field label="ENERGY">
            <select className={input} value={task.energy ?? ""} onChange={(e) => save({ energy: e.target.value || null })}>
              <option value="">not set</option>
              <option value="high">high</option>
              <option value="low">low</option>
            </select>
          </Field>
          <Field label="ESTIMATE (MIN)">
            <input
              key={`e${task.updated_at}`}
              type="number"
              min={0}
              className={input}
              defaultValue={task.estimate_min ?? ""}
              onBlur={(e) => save({ estimate_min: e.target.value ? Number(e.target.value) : null })}
            />
          </Field>
          <Field label="TOPIC">
            <input
              key={`t${task.updated_at}`}
              className={input}
              defaultValue={task.topic ?? ""}
              onBlur={(e) => (e.target.value || null) !== task.topic && save({ topic: e.target.value || null })}
            />
          </Field>
          <Field label="VISIBILITY">
            <select className={input} value={task.visibility} onChange={(e) => save({ visibility: e.target.value })}>
              <option value="team">team</option>
              <option value="private">private</option>
              <option value="public">public</option>
            </select>
          </Field>
          {task.status === "waiting" && (
            <Field label="FOLLOW UP">
              <input type="date" className={input} value={task.follow_up ?? ""} onChange={(e) => save({ follow_up: e.target.value || null })} />
            </Field>
          )}
        </div>

        <Field label="DEFINITION OF DONE">
          <textarea
            key={`d${task.updated_at}`}
            rows={2}
            defaultValue={task.definition_of_done ?? ""}
            onBlur={(e) => (e.target.value || null) !== task.definition_of_done && save({ definition_of_done: e.target.value || null })}
            className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent"
          />
        </Field>

        {!task.parent_id && (
          <div className="flex flex-col">
            <span className="flex items-baseline gap-2 pb-1.5">
              <span className="text-[13px] font-medium">Steps</span>
              <span className="cap">
                {task.steps?.filter((s) => s.status === "done").length ?? 0} of {task.steps?.length ?? 0} · who does what
              </span>
            </span>
            {task.steps?.map((s) => (
              <div key={s.id} className="flex items-center gap-2.5 border-t border-line py-2">
                <input
                  type="checkbox"
                  aria-label={`Complete ${s.title}`}
                  checked={s.status === "done"}
                  onChange={() => run(s.status === "done" ? tasksApi.update(s.ref, { status: "next" }) : tasksApi.complete(s.ref))}
                  className="h-[15px] w-[15px] accent-accent"
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
                placeholder="Add a step… “Draft the e-mail @ai”"
                className={`${input} flex-1`}
                aria-label="Add a step"
              />
            </form>
          </div>
        )}

        <Activity taskRef={task.ref} version={task.updated_at} />

        <div className="flex flex-wrap gap-2 border-t border-line pt-3">
          {task.status !== "done" && (
            <button type="button" className="btn" onClick={() => run(tasksApi.complete(task.ref))}>
              <Check size={14} /> Complete
            </button>
          )}
          {task.status !== "done" && (
            <AgentPicker task={task} trigger="button" align="left" onReassigned={() => run(Promise.resolve())} />
          )}
          {agentWork && task.status !== "done" && (
            <button
              type="button"
              className="btn"
              title="Record that you had to step in (for the HR agent)"
              onClick={() => {
                const n = window.prompt("What did you have to do?");
                if (n !== null) run(tasksApi.intervene(task.ref, n));
              }}
            >
              I stepped in
            </button>
          )}
          <button type="button" className="btn" onClick={async () => setHistory(history ? null : await tasksApi.history(task.ref))}>
            <History size={14} /> History
          </button>
          <button type="button" className="btn ml-auto" onClick={() => run(tasksApi.archive(task.ref)).then(onClose)}>
            <Archive size={14} /> Archive
          </button>
        </div>

        {history && (
          <ol className="flex flex-col">
            {[...history].reverse().map((h) => (
              <li key={h.version} className="flex items-center gap-2 border-t border-line py-1.5">
                <span className="cap w-7">v{h.version}</span>
                <span className="text-[12px]">{h.action}</span>
                <span className="cap truncate">
                  {h.actor_name} · {new Date(h.at).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" })}
                  {h.run_id ? ` · run ${h.run_id}` : ""}
                </span>
                {h.version < history.length && (
                  <button
                    type="button"
                    title={`Restore version ${h.version}`}
                    className="ml-auto text-ink-3 hover:text-accent"
                    onClick={() => run(tasksApi.restore(task.ref, h.version))}
                  >
                    <RotateCcw size={13} />
                  </button>
                )}
              </li>
            ))}
          </ol>
        )}
        {error && <p className="cap text-red-400!">{error}</p>}
      </div>
    </Panel>
  );
}
