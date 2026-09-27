import { useEffect, useState } from "react";
import { api } from "../api";
import { label, t } from "../i18n";
import { confirmDialog, toast } from "./overlay";
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

const input = "rounded border border-line bg-bg p-2 text-sm outline-none focus:border-accent";

/** Write feedback for a member, optionally about a task. */
export function FeedbackForm({ to, taskRef, onSent }: { to: string | number; taskRef?: string; onSent?: () => void }) {
  const [kind, setKind] = useState("critique");
  const [body, setBody] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap gap-2">
        <select className={`${input} h-8 py-0`} value={kind} onChange={(e) => setKind(e.target.value)} aria-label={t("fb.kind_aria")}>
          <option value="critique">{t("fb.kind.critique")}</option>
          <option value="suggestion">{t("fb.kind.suggestion")}</option>
          <option value="praise">{t("fb.kind.praise")}</option>
        </select>
        <button
          type="button"
          className="btn"
          disabled={!body.trim()}
          onClick={() =>
            feedbackApi.give(to, body.trim(), kind, taskRef).then(
              () => {
                setBody("");
                setMsg(t("fb.sent"));
                onSent?.();
              },
              (e) => setMsg(e.message),
            )
          }
        >
          {t("fb.give")}
        </button>
        {msg && <span className="self-center text-xs text-ink-2">{msg}</span>}
      </div>
      <textarea
        rows={2}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        placeholder={t("fb.placeholder")}
        aria-label={t("fb.aria")}
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
      <span className="text-xs text-ink-2">
        {label("fb.kind", f.kind)} · {other ?? "?"}
        {f.task_ref ? ` · ${f.task_ref}` : ""} · {label("fb.status", f.status)}
        {f.applied_ref ? ` → ${f.applied_ref}` : ""}
      </span>
      <span className="text-sm">{f.body}</span>
      {f.resolution && <span className="text-xs text-ink-2">{f.resolution}</span>}
      {f.status === "open" && other && (
        <span className="flex gap-2 pt-1">
          <button type="button" className="text-xs text-ink-2 hover:text-accent" onClick={() => feedbackApi.resolve(f.id, "applied", "").then(load, (e) => setError(e.message))}>
            {t("fb.mark_applied")}
          </button>
          <button
            type="button"
            className="text-xs text-ink-2 hover:text-accent"
            onClick={async () => {
              const why = await confirmDialog({ title: t("fb.dismiss_title"), reason: t("fb.dismiss_reason"), confirm: t("fb.dismiss"), danger: true });
              if (why === null) return;
              if (!why.trim()) return toast(t("fb.dismiss_need_reason"), { error: true });
              feedbackApi.resolve(f.id, "dismissed", why).then(load, (e) => setError(e.message));
            }}
          >
            {t("fb.dismiss")}
          </button>
        </span>
      )}
    </div>
  );
  return (
    <Panel title={t("fb.title")} right={t("fb.open_n", { n: received.filter((f) => f.status === "open").length })}>
      <div className="border-b border-line px-4 py-3">
        <FeedbackForm to={member.id} onSent={load} />
      </div>
      <p className="px-4 pt-2 text-xs font-medium text-ink-2">{t("fb.received")}</p>
      {received.length === 0 && <p className="px-4 py-2 text-xs text-ink-2">{t("fb.none")}</p>}
      {received.map((f) => row(f, f.from_name))}
      {given.length > 0 && <p className="px-4 pt-2 text-xs font-medium text-ink-2">{t("fb.given")}</p>}
      {given.map((f) => (
        <div key={`g${f.id}`} className="flex flex-col gap-0.5 border-b border-line px-4 py-2 last:border-0">
          <span className="text-xs text-ink-2">
            {label("fb.kind", f.kind)} → {f.to_name} · {label("fb.status", f.status)}
          </span>
          <span className="text-sm">{f.body}</span>
        </div>
      ))}
      {error && <p className="px-4 py-2 text-xs text-red-400">{error}</p>}
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
        {t("fb.edit_instructions")}
      </button>
    );
  return (
    <div className="flex flex-col gap-2 p-4">
      <textarea rows={12} value={text} onChange={(e) => setText(e.target.value)} aria-label={t("fb.instructions_aria")} className={`${input} font-mono text-xs`} />
      <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder={t("fb.why_ph")} className={`${input} h-8 py-0`} aria-label={t("fb.why_aria")} />
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-accent"
          disabled={text.trim().length < 40}
          onClick={() =>
            feedbackApi.proposeInstructions(agentId, text, reason).then(
              (r) => setMsg(t("fb.proposed", { task: r.task, assignee: r.assignee, path: r.path })),
              (e) => setMsg(e.message),
            )
          }
        >
          {t("fb.propose")}
        </button>
        <button type="button" className="btn" onClick={() => setOpen(false)}>
          {t("act.cancel")}
        </button>
        {msg && <span className="text-xs text-ink-2">{msg}</span>}
      </div>
    </div>
  );
}
