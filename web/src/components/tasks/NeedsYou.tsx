import { Check, CornerUpLeft, Send, Sparkles, X } from "lucide-react";
import { useState } from "react";
import { agentsApi } from "../../agentsApi";
import { label, t } from "../../i18n";
import { refreshNeedsMe } from "../../needsMeApi";
import { taskChanged, TaskLink } from "../../taskSheet";
import { type Related, type Task, tasksApi } from "../../tasksApi";
import { toast } from "../overlay";
import { type AskInfo, parseAsk } from "./text";

const field = "w-full rounded-md border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-accent";

function useAct(onDone: () => void) {
  const [busy, setBusy] = useState(false);
  const act = async (p: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await p();
      toast(done);
      taskChanged();
      window.dispatchEvent(new Event("pos:approvals"));
      refreshNeedsMe();
      onDone();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
    }
  };
  return { busy, act };
}

/** A question from an agent: the options (the recommended one marked) and a reply. Sending closes the ask. */
function AskCard({
  ticketRef,
  info,
  asker,
  showRef,
  autoFocus,
  onDone,
}: {
  ticketRef: string;
  info: AskInfo;
  asker: string;
  showRef: boolean;
  autoFocus: boolean;
  onDone: () => void;
}) {
  const [choice, setChoice] = useState<number | null>(null);
  const [text, setText] = useState("");
  const { busy, act } = useAct(onDone);
  const answer = [choice !== null ? info.options[choice].text : "", text.trim()].filter(Boolean).join(" — ");
  const send = () => answer && act(() => tasksApi.complete(ticketRef, answer), t("tk.needs.sent", { who: asker }));
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <p className="text-[15px] leading-snug text-ink">
          <span className="text-ink-2">{t("tk.needs.asks", { who: asker })} </span>
          <span className="font-medium">{info.question}</span>
        </p>
        {info.why && <p className="text-[13px] leading-relaxed text-ink-2">{t("tk.needs.why", { why: info.why.replace(/\.$/, "") })}</p>}
        {(showRef || info.blocking) && (
          <p className="text-xs text-ink-2">
            {info.blocking ? t("tk.needs.blocking") : ""}
            {showRef && (
              <>
                {info.blocking ? " · " : ""}
                <TaskLink taskRef={ticketRef} className="text-accent hover:underline">
                  {t("tk.needs.open_ticket", { ref: ticketRef })}
                </TaskLink>
              </>
            )}
          </p>
        )}
      </div>
      {info.options.length > 0 && (
        <div role="radiogroup" aria-label={t("tk.needs.options")} className="flex flex-col gap-2">
          {info.options.map((o, i) => (
            <button
              key={o.text}
              type="button"
              role="radio"
              aria-checked={choice === i}
              onClick={() => setChoice(choice === i ? null : i)}
              className={`flex min-h-11 items-center gap-3 rounded-md border px-3 py-2 text-left text-sm transition ${
                choice === i
                  ? "border-accent bg-accent/10 text-ink"
                  : o.recommended
                    ? "border-emerald-400/50 bg-emerald-400/5 hover:border-emerald-300"
                    : "border-line hover:border-ink-3"
              }`}
            >
              <span
                aria-hidden
                className={`grid h-4 w-4 shrink-0 place-items-center rounded-full border ${choice === i ? "border-accent" : "border-ink-3"}`}
              >
                {choice === i && <span className="h-2 w-2 rounded-full bg-accent" />}
              </span>
              <span className="min-w-0 flex-1 break-words">{o.text}</span>
              {o.recommended && (
                <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-emerald-400/15 px-2 py-0.5 text-xs text-emerald-200">
                  <Sparkles size={12} aria-hidden /> {t("tk.needs.recommended")}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
      {info.options.length === 0 && info.recommendation && (
        <p className="rounded-md border border-emerald-400/40 bg-emerald-400/5 px-3 py-2 text-[13px] text-emerald-100">
          <span className="font-medium">{t("tk.needs.recommends")} </span>
          {info.recommendation}
        </p>
      )}
      <form
        className="flex flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <label htmlFor={`answer-${ticketRef}`} className="sr-only">
          {t("tk.needs.answer_label")}
        </label>
        <textarea
          id={`answer-${ticketRef}`}
          data-needs-focus={autoFocus ? "" : undefined}
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
          }}
          placeholder={info.options.length ? t("tk.needs.answer_note") : t("tk.needs.answer_placeholder")}
          className={field}
        />
        <span className="flex flex-wrap items-center gap-2">
          <button type="submit" className="btn-accent h-9!" disabled={busy || !answer}>
            <Send size={14} /> {t("tk.needs.send")}
          </button>
          <span className="text-xs text-ink-2">{t("tk.needs.send_hint", { who: asker })}</span>
        </span>
      </form>
    </div>
  );
}

function ReviewCard({ task, autoFocus, onDone }: { task: Task; autoFocus: boolean; onDone: () => void }) {
  const [returning, setReturning] = useState(false);
  const [text, setText] = useState("");
  const { busy, act } = useAct(onDone);
  const who = task.assignee_name ?? t("tk.someone");
  return (
    <div className="flex flex-col gap-3">
      <p className="text-[15px] leading-snug">{t("tk.needs.review", { who })}</p>
      {returning ? (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) act(() => tasksApi.review(task.ref, false, text.trim()), t("tk.needs.returned"));
          }}
        >
          <label htmlFor="return-note" className="sr-only">
            {t("tk.needs.return_label")}
          </label>
          <textarea
            id="return-note"
            autoFocus
            rows={3}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t("tk.needs.return_placeholder")}
            className={field}
          />
          <span className="flex flex-wrap gap-2">
            <button type="submit" className="btn-accent h-9!" disabled={busy || !text.trim()}>
              <CornerUpLeft size={14} /> {t("tk.needs.return_send")}
            </button>
            <button type="button" className="btn h-9!" onClick={() => setReturning(false)}>
              {t("act.cancel")}
            </button>
          </span>
        </form>
      ) : (
        <span className="flex flex-wrap gap-2">
          <button
            type="button"
            data-needs-focus={autoFocus ? "" : undefined}
            className="btn-accent h-9!"
            disabled={busy}
            onClick={() => act(() => tasksApi.review(task.ref, true), t("tk.needs.accepted"))}
          >
            <Check size={15} /> {t("act.approve")}
          </button>
          <button type="button" className="btn h-9!" disabled={busy} onClick={() => setReturning(true)}>
            <CornerUpLeft size={14} /> {t("tk.needs.return")}
          </button>
        </span>
      )}
    </div>
  );
}

function detailText(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** An approval: what exactly is to happen, and Schválit / Zamítnout. */
export function ApprovalCard({
  a,
  autoFocus,
  onDone,
}: {
  a: Related["approvals"][number];
  autoFocus: boolean;
  onDone: () => void;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [text, setText] = useState("");
  const { busy, act } = useAct(onDone);
  const details = Object.entries(a.details ?? {}).filter(([, v]) => v !== null && v !== "");
  return (
    <div className="flex flex-col gap-3">
      <p className="text-[15px] leading-snug">
        <span className="text-ink-2">{t("tk.needs.approval", { who: a.requested_by_name ?? t("tk.someone") })} </span>
        <span className="font-medium">{label("approval", a.action)}</span>
      </p>
      {a.why && <p className="text-[13px] text-ink-2">{a.why}</p>}
      {details.length > 0 && (
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1.5 rounded-md border border-line bg-bg p-3 text-[13px] sm:grid-cols-[120px_minmax(0,1fr)]">
          {details.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-ink-2">{label("appr.field", k)}</dt>
              <dd className="min-w-0 break-words whitespace-pre-wrap">{detailText(v)}</dd>
            </div>
          ))}
        </dl>
      )}
      {rejecting ? (
        <form
          className="flex flex-col gap-2 sm:flex-row"
          onSubmit={(e) => {
            e.preventDefault();
            act(() => agentsApi.decide(a.id, false, text.trim() || undefined), t("tk.needs.rejected"));
          }}
        >
          <label htmlFor={`reject-${a.id}`} className="sr-only">
            {t("tk.needs.reject_label")}
          </label>
          <input
            id={`reject-${a.id}`}
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t("tk.needs.reject_placeholder")}
            className={`${field} h-9 py-0 sm:flex-1`}
          />
          <span className="flex gap-2">
            <button type="submit" className="btn h-9! border-amber-400/70! text-amber-200!" disabled={busy}>
              <X size={14} /> {t("act.reject")}
            </button>
            <button type="button" className="btn h-9!" onClick={() => setRejecting(false)}>
              {t("act.cancel")}
            </button>
          </span>
        </form>
      ) : (
        <span className="flex flex-wrap gap-2">
          <button
            type="button"
            data-needs-focus={autoFocus ? "" : undefined}
            className="btn-accent h-9!"
            disabled={busy}
            onClick={() => act(() => agentsApi.decide(a.id, true), t("tk.needs.approved"))}
          >
            <Check size={15} /> {t("act.approve")}
          </button>
          <button type="button" className="btn h-9!" disabled={busy} onClick={() => setRejecting(true)}>
            <X size={14} /> {t("act.reject")}
          </button>
        </span>
      )}
    </div>
  );
}

/** Does the task wait for the signed-in person (an ask, a result to review, an approval)? */
export function needsYou(task: Task, related: Related | null, meId: number | null): boolean {
  return (
    (task.source === "ask_owner" && task.status !== "done" && task.assignee_id === meId) ||
    (task.status === "review" && !!task.can_review) ||
    !!related?.asks_open.length ||
    !!related?.approvals.length
  );
}

/** "Co potřebuju od tebe": the first thing in the panel when the task waits for you. */
export default function NeedsYou({
  task,
  related,
  meId,
  onDone,
}: {
  task: Task;
  related: Related | null;
  meId: number | null;
  onDone: () => void;
}) {
  if (!needsYou(task, related, meId)) return null;
  const blocks = [];
  let first = true;
  const focus = () => {
    const f = first;
    first = false;
    return f;
  };
  if (task.source === "ask_owner" && task.status !== "done" && task.assignee_id === meId) {
    const info = parseAsk(task.notes, task.title);
    blocks.push(
      <AskCard
        key="self"
        ticketRef={task.ref}
        info={info}
        asker={related?.ask_for?.asker_name ?? info.asker ?? t("tk.someone")}
        showRef={false}
        autoFocus={focus()}
        onDone={onDone}
      />,
    );
  }
  for (const a of related?.asks_open ?? [])
    blocks.push(
      <AskCard
        key={a.ref}
        ticketRef={a.ref}
        info={{ ...parseAsk(a.notes, a.title), blocking: a.blocking }}
        asker={a.asker_name}
        showRef
        autoFocus={focus()}
        onDone={onDone}
      />,
    );
  if (task.status === "review" && task.can_review) blocks.push(<ReviewCard key="review" task={task} autoFocus={focus()} onDone={onDone} />);
  for (const a of related?.approvals ?? []) blocks.push(<ApprovalCard key={`a${a.id}`} a={a} autoFocus={focus()} onDone={onDone} />);

  return (
    <section
      id="needs-you"
      aria-labelledby="needs-you-title"
      className="flex scroll-mt-4 flex-col gap-4 rounded-lg border border-orange-400/50 bg-orange-400/[0.06] p-4 sm:p-5"
    >
      <h3 id="needs-you-title" className="flex items-center gap-2 text-sm font-medium text-orange-200">
        <span aria-hidden className="h-2 w-2 rounded-full bg-orange-300" />
        {t("tk.needs.title")}
      </h3>
      {blocks.map((b, i) => (
        <div key={i} className={i ? "border-t border-orange-400/20 pt-4" : ""}>
          {b}
        </div>
      ))}
    </section>
  );
}
