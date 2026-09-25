import { useEffect, useState } from "react";
import { api } from "../api";
import { Panel } from "./ui";

export type FeedbackItem = {
  id: number;
  from_id: number;
  from_name: string | null;
  to_id: number;
  to_name: string | null;
  task_ref: string | null;
  kind: "praise" | "critique" | "suggestion";
  body: string;
  status: "open" | "applied" | "dismissed";
  resolution: string | null;
  applied_ref: string | null;
  created_at: string;
};

const post = <T,>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const feedbackApi = {
  list: (q: { to_id?: number; from_id?: number; status?: string }) =>
    api<FeedbackItem[]>(`/api/feedback?${new URLSearchParams(Object.entries(q).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)]))}`),
  give: (to: string | number, body: string, kind: string, task_id?: string) =>
    post<FeedbackItem>("/api/feedback", { to, body, kind, ...(task_id ? { task_id } : {}) }),
  resolve: (id: number, status: "applied" | "dismissed", note: string) =>
    post<FeedbackItem>(`/api/feedback/${id}/resolve`, { status, note }),
  proposeInstructions: (agentId: number, text: string, reason: string) =>
    post<{ task: string; path: string; assignee: string }>(`/api/agents/${agentId}/instructions`, { text, reason }),
};

const input = "rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent";

/** Write feedback for a member, optionally about a task. */
export function FeedbackForm({ to, taskRef, onSent }: { to: string | number; taskRef?: string; onSent?: () => void }) {
  const [kind, setKind] = useState("critique");
  const [body, setBody] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex gap-2">
        <select className={`${input} h-8 py-0`} value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind">
          <option value="critique">critique</option>
          <option value="suggestion">suggestion</option>
          <option value="praise">praise</option>
        </select>
        <button
          type="button"
          className="btn"
          disabled={!body.trim()}
          onClick={() =>
            feedbackApi.give(to, body.trim(), kind, taskRef).then(
              () => {
                setBody("");
                setMsg("sent · it reaches their inbox and their next runs");
                onSent?.();
              },
              (e) => setMsg(e.message),
            )
          }
        >
          Give feedback
        </button>
        {msg && <span className="cap self-center">{msg}</span>}
      </div>
      <textarea
        rows={2}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder="What happened, why it matters, what to do instead"
        aria-label="Feedback"
        className={input}
      />
    </div>
  );
}

/** A member's profile: feedback received and given, what came of it, and new feedback. */
export function FeedbackPanel({ member }: { member: { id: number; name: string } }) {
  const [received, setReceived] = useState<FeedbackItem[]>([]);
  const [given, setGiven] = useState<FeedbackItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = () => {
    feedbackApi.list({ to_id: member.id }).then(setReceived, (e) => setError(e.message));
    feedbackApi.list({ from_id: member.id }).then(setGiven, () => undefined);
  };
  useEffect(load, [member.id]);
  const row = (f: FeedbackItem, other: string | null) => (
    <div key={f.id} className="flex flex-col gap-0.5 border-b border-line px-4 py-2 last:border-0">
      <span className="cap">
        {f.kind} · {other ?? "?"}
        {f.task_ref ? ` · ${f.task_ref}` : ""} · {f.status}
        {f.applied_ref ? ` → ${f.applied_ref}` : ""}
      </span>
      <span className="text-[13px]">{f.body}</span>
      {f.resolution && <span className="text-xs text-ink-3">{f.resolution}</span>}
      {f.status === "open" && other && (
        <span className="flex gap-2 pt-1">
          <button type="button" className="cap hover:text-accent" onClick={() => feedbackApi.resolve(f.id, "applied", "").then(load, (e) => setError(e.message))}>
            mark applied
          </button>
          <button
            type="button"
            className="cap hover:text-accent"
            onClick={() => {
              const why = window.prompt("Why dismiss it?");
              if (why) feedbackApi.resolve(f.id, "dismissed", why).then(load, (e) => setError(e.message));
            }}
          >
            dismiss
          </button>
        </span>
      )}
    </div>
  );
  return (
    <Panel fig="FEEDBACK" title="Feedback" right={`${received.filter((f) => f.status === "open").length} open`}>
      <div className="border-b border-line px-4 py-3">
        <FeedbackForm to={member.id} onSent={load} />
      </div>
      <p className="cap px-4 pt-2">received</p>
      {received.length === 0 && <p className="cap px-4 py-2">none yet</p>}
      {received.map((f) => row(f, f.from_name))}
      {given.length > 0 && <p className="cap px-4 pt-2">given</p>}
      {given.map((f) => (
        <div key={`g${f.id}`} className="flex flex-col gap-0.5 border-b border-line px-4 py-2 last:border-0">
          <span className="cap">
            {f.kind} → {f.to_name} · {f.status}
          </span>
          <span className="text-[13px]">{f.body}</span>
        </div>
      ))}
      {error && <p className="cap px-4 py-2 text-red-400!">{error}</p>}
    </Panel>
  );
}

/** Propose a new version of an agent's instructions (a task for the Dev agent, checked by the deployer). */
export function InstructionsEditor({ agentId, current }: { agentId: number; current: string | null }) {
  const [text, setText] = useState(current ?? "");
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  if (!open)
    return (
      <button type="button" className="btn m-4" onClick={() => setOpen(true)}>
        Edit instructions…
      </button>
    );
  return (
    <div className="flex flex-col gap-2 p-4">
      <textarea rows={12} value={text} onChange={(e) => setText(e.target.value)} aria-label="Instructions" className={`${input} font-mono text-[11px]`} />
      <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why (e.g. the feedback it follows)" className={`${input} h-8 py-0`} aria-label="Why" />
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="btn-accent"
          disabled={text.trim().length < 40}
          onClick={() =>
            feedbackApi.proposeInstructions(agentId, text, reason).then(
              (r) => setMsg(`${r.task} for ${r.assignee}: commits ${r.path}, the deployer checks it`),
              (e) => setMsg(e.message),
            )
          }
        >
          Propose
        </button>
        <button type="button" className="btn" onClick={() => setOpen(false)}>
          Cancel
        </button>
        {msg && <span className="cap">{msg}</span>}
      </div>
    </div>
  );
}
