import { CheckCircle2, ChevronDown, ChevronRight, Circle, CircleCheck, Pencil, RotateCcw, Send } from "lucide-react";
import { ToolText } from "../../toolMarkup";
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { LOCALE, label, t } from "../../i18n";
import { taskChanged } from "../../taskSheet";
import { type Actor, type Comment, type Task, type TaskLive as Live, type Version, tasksApi } from "../../tasksApi";
import { FeedbackForm } from "../Feedback";
import Markdown from "../Markdown";
import { toast } from "../overlay";
import { Avatar, statusText } from "./bits";
import TaskLive from "./TaskLive";
import { doneItems, sections } from "./text";

/* ------------------------------------------------------------------ shared */

/** Long content folds to `limit` px with "Zobrazit víc". */
export function Collapsible({ children, limit = 280, deps }: { children: ReactNode; limit?: number; deps?: unknown }) {
  const box = useRef<HTMLDivElement>(null);
  const [tall, setTall] = useState(false);
  const [open, setOpen] = useState(false);
  useLayoutEffect(() => {
    const el = box.current;
    if (el) setTall(el.scrollHeight > limit + 80);
  }, [deps, limit]);
  return (
    <div className="flex flex-col">
      <div
        ref={box}
        className={`relative ${tall && !open ? "overflow-hidden" : ""}`}
        style={tall && !open ? { maxHeight: limit } : undefined}
      >
        {children}
        {tall && !open && <div aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-surface to-transparent" />}
      </div>
      {tall && (
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          className="mt-1.5 inline-flex items-center gap-1 self-start rounded text-[13px] text-accent hover:underline"
        >
          <ChevronDown size={14} className={open ? "rotate-180" : ""} /> {open ? t("tk.less") : t("tk.more")}
        </button>
      )}
    </div>
  );
}

function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-center gap-2 pb-2">
      <h4 className="text-xs font-medium tracking-wide text-ink-2 uppercase">{children}</h4>
      {right && <span className="ml-auto">{right}</span>}
    </div>
  );
}

const dayKey = (iso: string) => new Date(iso).toLocaleDateString("sv-SE");
function dayLabel(iso: string): string {
  const d = dayKey(iso);
  const today = dayKey(new Date().toISOString());
  const y = dayKey(new Date(Date.now() - 864e5).toISOString());
  if (d === today) return t("tk.day.today");
  if (d === y) return t("tk.day.yesterday");
  return new Date(iso).toLocaleDateString(LOCALE, { weekday: "long", day: "numeric", month: "long" });
}
const hm = (iso: string) => new Date(iso).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
const stamp = (iso: string) => new Date(iso).toLocaleString(LOCALE, { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });

/* ------------------------------------------------------------------ Přehled */

function Description({ task, onSave }: { task: Task; onSave: (notes: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(task.notes ?? "");
  useEffect(() => {
    setEditing(false);
    setDraft(task.notes ?? "");
  }, [task.ref, task.updated_at]);
  const parts = sections(task.notes ?? "");
  const empty = !(task.notes ?? "").trim();
  const edit = (
    <button type="button" className="inline-flex items-center gap-1 text-xs text-accent hover:underline" onClick={() => setEditing(true)}>
      <Pencil size={12} /> {empty ? t("tk.desc.add") : t("act.edit")}
    </button>
  );
  if (editing)
    return (
      <form
        className="flex flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          onSave(draft);
          setEditing(false);
        }}
      >
        <SectionTitle>{t("tk.desc.title")}</SectionTitle>
        <textarea
          autoFocus
          rows={12}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          aria-label={t("tk.desc.title")}
          className="rounded-md border border-line bg-bg p-3 font-mono text-[13px] leading-relaxed outline-none focus:border-accent"
        />
        <span className="flex gap-2">
          <button type="submit" className="btn-accent">
            {t("act.save")}
          </button>
          <button type="button" className="btn" onClick={() => setEditing(false)}>
            {t("act.cancel")}
          </button>
        </span>
      </form>
    );
  return (
    <section className="flex flex-col">
      <SectionTitle right={edit}>{t("tk.desc.title")}</SectionTitle>
      {empty ? (
        <p className="text-sm text-ink-2 italic">{t("tk.desc.empty")}</p>
      ) : (
        <div className="flex flex-col gap-4">
          {parts.map((p, i) => (
            <div key={i} className="flex min-w-0 flex-col gap-1">
              {p.heading && <h5 className="text-[15px] font-medium text-ink">{p.heading}</h5>}
              {p.body && (
                <Collapsible limit={220} deps={p.body}>
                  <Markdown text={p.body} className="text-[14px]" />
                </Collapsible>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function DoneWhen({ task }: { task: Task }) {
  const items = doneItems(task.definition_of_done);
  if (!items.length) return null;
  const done = task.status === "done";
  return (
    <section className="rounded-lg border border-line bg-bg/60 p-4">
      <SectionTitle>{t("tk.dod")}</SectionTitle>
      <ul className="flex flex-col gap-2">
        {items.map((it) => (
          <li key={it} className="flex items-start gap-2.5 text-[14px] leading-snug">
            {done ? (
              <CircleCheck size={17} className="mt-px shrink-0 text-emerald-300" aria-hidden />
            ) : (
              <Circle size={17} className="mt-px shrink-0 text-ink-3" aria-hidden />
            )}
            <span className="min-w-0 break-words">{it}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Steps({ task, onChange }: { task: Task; onChange: () => void }) {
  const [step, setStep] = useState("");
  if (task.parent_id) return null;
  const steps = task.steps ?? [];
  const run = (p: Promise<unknown>) =>
    p.then(
      () => {
        onChange();
        taskChanged();
      },
      (e) => toast(e.message, { error: true }),
    );
  return (
    <section className="flex flex-col">
      <SectionTitle right={steps.length ? <span className="text-xs text-ink-2">{t("tk.steps.count", { done: task.steps_done ?? 0, total: steps.length })}</span> : null}>
        {t("tk.steps.title")}
      </SectionTitle>
      <ul className="flex flex-col">
        {steps.map((s) => (
          <li key={s.id} className="flex min-w-0 items-center gap-2.5 border-b border-line py-2 last:border-0">
            <input
              type="checkbox"
              aria-label={t("work.row.complete", { title: s.title })}
              checked={s.status === "done"}
              onChange={() => run(s.status === "done" ? tasksApi.update(s.ref, { status: "next" }) : tasksApi.complete(s.ref))}
              className="h-4 w-4 shrink-0 accent-accent"
            />
            <span className={`min-w-0 flex-1 truncate text-sm ${s.status === "done" ? "text-ink-3 line-through" : ""}`}>{s.title}</span>
            <Avatar type={s.assignee_type} name={s.assignee_name} size={22} />
          </li>
        ))}
      </ul>
      <form
        className="pt-1"
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
          placeholder={t("tk.steps.add")}
          aria-label={t("tk.steps.add")}
          className="h-9 w-full rounded-md border border-transparent bg-transparent px-2 text-sm outline-none placeholder:text-ink-3 hover:border-line focus:border-accent"
        />
      </form>
    </section>
  );
}

export function Overview({ task, onSave, onChange, hideResult = false }: { task: Task; onSave: (c: Record<string, unknown>) => void; onChange: () => void; hideResult?: boolean }) {
  // hideResult: the owner's report above shows the result (with the raw text in its details).
  const result = !hideResult && !!task.progress_note?.trim() && (task.status === "review" || task.status === "done");
  return (
    <div className="flex flex-col gap-7">
      {result && (
        <section className="rounded-lg border border-emerald-400/30 bg-emerald-400/[0.04] p-4">
          <SectionTitle>
            <span className="inline-flex items-center gap-1.5 text-emerald-200">
              <CheckCircle2 size={14} aria-hidden /> {task.status === "done" ? t("tk.result") : t("tk.result_review")}
            </span>
          </SectionTitle>
          <Collapsible limit={320} deps={task.progress_note}>
            <Markdown text={task.progress_note!} className="text-[14px]" />
          </Collapsible>
        </section>
      )}
      {!result && !hideResult && task.status === "working" && task.progress_note && (
        <p className="rounded-lg border border-cyan-400/30 bg-cyan-400/[0.04] px-4 py-3 text-[14px]">
          <span className="text-cyan-200">{t("tk.now")} </span>
          <ToolText text={task.progress_note} />
        </p>
      )}
      <Description task={task} onSave={(notes) => notes !== task.notes && onSave({ notes })} />
      <DoneWhen task={task} />
      <Steps task={task} onChange={onChange} />
    </div>
  );
}

/* ------------------------------------------------------------------ Diskuse */

const TALK: Comment["kind"][] = ["comment", "return", "review", "progress"];
export const isTalk = (c: Comment) => TALK.includes(c.kind);

export function Discussion({ task, comments, meId, onSent }: { task: Task; comments: Comment[] | null; meId: number | null; onSent: () => void }) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const seen = useRef<number | null>(null);
  const talk = (comments ?? []).filter(isTalk);
  // After a new message (not on opening the tab), keep the newest in view.
  useEffect(() => {
    if (!comments) return;
    if (seen.current !== null && talk.length > seen.current) end.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    seen.current = talk.length;
  }, [talk.length, comments]);
  const send = async () => {
    if (!draft.trim()) return;
    setBusy(true);
    try {
      await tasksApi.comment(task.ref, draft.trim());
      setDraft("");
      onSent();
      taskChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
    }
  };
  let lastDay = "";
  return (
    <div className="flex flex-col gap-4">
      {comments && talk.length === 0 && <p className="py-6 text-center text-sm text-ink-2">{t("tk.talk.empty")}</p>}
      <ol className="flex flex-col gap-3">
        {talk.map((c) => {
          const mine = c.author_id === meId;
          const day = dayKey(c.created_at);
          const sep = day !== lastDay;
          lastDay = day;
          const tag = c.kind === "return" ? t("tk.talk.return") : c.kind === "review" ? t("tk.talk.review") : c.kind === "progress" ? t("tk.talk.progress") : null;
          const type = (c.author_kind === "human" ? "human" : c.author_kind === "ai" ? "ai" : "agent") as "human" | "ai" | "agent";
          return (
            <li key={c.id} className="flex flex-col gap-3">
              {sep && (
                <span className="flex items-center gap-3 text-xs text-ink-2">
                  <span className="h-px flex-1 bg-line" />
                  {dayLabel(c.created_at)}
                  <span className="h-px flex-1 bg-line" />
                </span>
              )}
              <div className={`flex min-w-0 items-end gap-2.5 ${mine ? "flex-row-reverse" : ""}`}>
                {!mine && <Avatar type={type} name={c.author_name} size={28} />}
                <div
                  className={`flex max-w-[min(640px,88%)] min-w-0 flex-col gap-1 rounded-2xl px-3.5 py-2.5 ${
                    mine ? "rounded-br-sm bg-accent/15" : "rounded-bl-sm border border-line bg-raised"
                  }`}
                >
                  <span className="flex flex-wrap items-baseline gap-x-2 text-xs text-ink-2">
                    {!mine && <span className="font-medium text-ink">{c.author_name ?? t("tk.system")}</span>}
                    {tag && <span className="rounded-full bg-bg/70 px-1.5 text-ink-2">{tag}</span>}
                    <span>{hm(c.created_at)}</span>
                  </span>
                  <Collapsible limit={260} deps={c.body}>
                    <Markdown text={c.body.replace(/^\d{1,3} %:\s*/, "")} compact className="text-[14px]" />
                  </Collapsible>
                </div>
              </div>
            </li>
          );
        })}
      </ol>
      <div ref={end} />
      <form
        className="sticky bottom-0 flex flex-col gap-2 border-t border-line bg-surface pt-3 pb-1"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <label htmlFor="talk-reply" className="sr-only">
          {t("tk.talk.reply")}
        </label>
        <div className="flex items-end gap-2">
          <textarea
            id="talk-reply"
            rows={2}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
            }}
            placeholder={t("tk.talk.placeholder")}
            className="min-w-0 flex-1 resize-y rounded-md border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <button type="submit" className="btn-accent h-10!" disabled={busy || !draft.trim()} aria-label={t("tk.talk.send")}>
            <Send size={15} /> <span className="hidden sm:inline">{t("tk.talk.send")}</span>
          </button>
        </div>
        <span className="text-xs text-ink-2">{t("tk.talk.hint")}</span>
      </form>
    </div>
  );
}

/* ------------------------------------------------------------------ Historie */

type Event = { at: string; who: string | null; text: string; detail?: string; system: boolean };

const FIELD_WORD: Record<string, string> = {
  title: "tk.f.title",
  notes: "tk.f.notes",
  priority: "tk.f.priority",
  deadline: "tk.f.deadline",
  do_date: "tk.f.do_date",
  topic: "tk.f.topic",
  definition_of_done: "tk.f.dod",
  reviewer_id: "tk.f.reviewer",
  visibility: "tk.f.visibility",
};

function versionEvents(history: Version[], actors: Actor[]): Event[] {
  const name = (id: unknown) => {
    if (id == null) return t("who.unassigned");
    const a = actors.find((x) => x.id === id);
    return a ? (a.is_owner ? t("who.me") : a.name) : `#${id}`;
  };
  const out: Event[] = [];
  let prev: Version["data"] | undefined;
  for (const v of history) {
    const d = v.data ?? {};
    if (!prev) out.push({ at: v.at, who: v.actor_name, text: t("tk.h.created"), system: false });
    else {
      if (d.status !== prev.status && d.status)
        out.push({ at: v.at, who: v.actor_name, text: t("tk.h.status", { from: statusText(prev.status!), to: statusText(d.status) }), system: false });
      if (d.assignee_id !== prev.assignee_id)
        out.push({ at: v.at, who: v.actor_name, text: t("tk.h.assignee", { to: d.assignee_name ?? name(d.assignee_id) }), system: false });
      const changed = Object.keys(FIELD_WORD).filter((k) => JSON.stringify((d as Record<string, unknown>)[k]) !== JSON.stringify((prev as Record<string, unknown>)[k]));
      if (changed.length) out.push({ at: v.at, who: v.actor_name, text: t("tk.h.fields", { what: changed.map((k) => t(FIELD_WORD[k])).join(", ") }), system: true });
      if (d.progress_note && d.progress_note !== prev.progress_note && d.status === prev.status)
        out.push({ at: v.at, who: v.actor_name, text: t("tk.h.progress"), detail: d.progress_note, system: true });
    }
    prev = d;
  }
  return out;
}

function EventRow({ e }: { e: Event }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="flex min-w-0 gap-3 py-1.5 text-[13px]">
      <span className="w-12 shrink-0 pt-px text-xs text-ink-2 tabular-nums">{hm(e.at)}</span>
      <span aria-hidden className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${e.system ? "bg-dim" : "bg-accent"}`} />
      <span className="flex min-w-0 flex-1 flex-col gap-1">
        <span className={`min-w-0 ${e.system ? "text-ink-2" : "text-ink"}`}>
          {e.who && <span className="font-medium">{e.who === "Owner" ? t("who.me") : e.who} · </span>}
          {e.text}
          {e.detail && (
            <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="ml-1.5 inline-flex items-center text-xs text-accent hover:underline">
              <ChevronRight size={12} className={open ? "rotate-90" : ""} /> {open ? t("tk.less") : t("tk.detail")}
            </button>
          )}
        </span>
        {!open && e.detail && e.system && <span className="truncate text-xs text-ink-2">{e.detail.replace(/\s+/g, " ").slice(0, 160)}</span>}
        {open && e.detail && (
          <div className="rounded-md border border-line bg-bg p-3">
            <Markdown text={e.detail} compact className="text-[13px]" />
          </div>
        )}
      </span>
    </li>
  );
}

export function HistoryTab({ history, comments, actors }: { history: Version[] | null; comments: Comment[] | null; actors: Actor[] }) {
  if (!history || !comments) return <p className="py-6 text-sm text-ink-2">{t("act.loading")}</p>;
  const events: Event[] = [
    ...versionEvents(history, actors),
    ...comments
      .filter((c) => !isTalk(c))
      .map((c) => {
        const handoff = c.kind === "handoff";
        const first = c.body.split("\n")[0];
        return {
          at: c.created_at,
          who: c.author_name,
          text: handoff ? t("tk.h.handoff") : first.length > 140 ? `${first.slice(0, 139)}…` : first,
          detail: handoff || c.body.length > 140 || c.body.includes("\n") ? c.body : undefined,
          system: !handoff,
        };
      }),
  ].sort((a, b) => b.at.localeCompare(a.at));
  const days: { day: string; items: Event[] }[] = [];
  for (const e of events) {
    const d = dayKey(e.at);
    if (days[days.length - 1]?.day !== d) days.push({ day: d, items: [] });
    days[days.length - 1].items.push(e);
  }
  return (
    <div className="flex flex-col gap-5">
      {days.map((g) => (
        <section key={g.day}>
          <h4 className="pb-1 text-xs font-medium text-ink-2 first-letter:uppercase">{dayLabel(g.items[0].at)}</h4>
          <ol className="flex flex-col">{g.items.map((e, i) => <EventRow key={i} e={e} />)}</ol>
        </section>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ Technické */

function Kv({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(110px,auto)_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-[13px]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-ink-2">{k}</dt>
          <dd className="min-w-0 font-mono text-xs break-all text-ink">{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Technical({
  task,
  live,
  history,
  onRestore,
  onChange,
}: {
  task: Task;
  live: Live | null;
  history: Version[] | null;
  onRestore: (v: number) => void;
  onChange: () => void;
}) {
  const u = task.usage;
  return (
    <div className="flex flex-col gap-7">
      <section>
        <SectionTitle>{t("tk.tech.cost")}</SectionTitle>
        <div className="grid grid-cols-3 gap-3">
          {[
            [t("tk.tech.runs"), u?.runs ?? 0],
            [t("tk.tech.tokens"), (u?.tokens ?? 0).toLocaleString(LOCALE)],
            [t("tk.tech.usd"), `$${(u?.cost_usd ?? 0).toFixed(2)}`],
          ].map(([k, v]) => (
            <div key={String(k)} className="rounded-md border border-line bg-bg p-3">
              <div className="text-xs text-ink-2">{k}</div>
              <div className="font-mono text-lg">{v}</div>
            </div>
          ))}
        </div>
      </section>
      <TaskLive taskRef={task.ref} version={task.updated_at} onChange={onChange} />
      {live && live.runs.length > 0 && (
        <section>
          <SectionTitle>{t("tk.tech.run_list")}</SectionTitle>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-left text-[13px]">
              <thead className="text-xs text-ink-2">
                <tr>
                  <th className="py-1.5 pr-3 font-normal">#</th>
                  <th className="py-1.5 pr-3 font-normal">{t("tk.tech.who")}</th>
                  <th className="py-1.5 pr-3 font-normal">{t("tk.tech.status")}</th>
                  <th className="py-1.5 pr-3 font-normal">{t("tk.tech.engine")}</th>
                  <th className="py-1.5 pr-3 font-normal">{t("tk.tech.start")}</th>
                </tr>
              </thead>
              <tbody>
                {live.runs.map((r) => (
                  <tr key={r.id} className="border-t border-line align-top">
                    <td className="py-1.5 pr-3 font-mono text-xs">{r.id}</td>
                    <td className="py-1.5 pr-3">{r.actor_name}</td>
                    <td className="py-1.5 pr-3">
                      {label("runstatus", r.status)}
                      {r.detail && <div className="max-w-[320px] truncate text-xs text-ink-2" title={r.detail}>{r.detail}</div>}
                    </td>
                    <td className="py-1.5 pr-3 font-mono text-xs">{r.label ?? r.engine ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-xs text-ink-2">{stamp(r.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <section>
        <SectionTitle>{t("tk.tech.ids")}</SectionTitle>
        <Kv
          rows={[
            ["ref / id", `${task.ref} / ${task.id}`],
            [t("tk.tech.source"), task.source],
            ["status", task.status],
            ["assignee", `${task.assignee_type ?? "—"} #${task.assignee_id ?? "—"}`],
            ["reviewer", `#${task.reviewer_effective_id ?? "—"}${task.reviewer_id ? "" : " (default)"}`],
            ["value_kind", `${task.value_kind ?? "auto"} → ${task.value_kind_effective ?? "—"}`],
            ["visibility", task.visibility],
            ["returned / interventions", `${task.returned_count} / ${task.interventions}`],
            ["created_at", task.created_at],
            ["updated_at", task.updated_at],
            ["completed_at", task.completed_at ?? "—"],
          ]}
        />
      </section>
      {history && history.length > 1 && (
        <section>
          <SectionTitle>{t("tk.tech.versions")}</SectionTitle>
          <ol className="flex flex-col">
            {[...history].reverse().map((h) => (
              <li key={h.version} className="flex min-w-0 items-center gap-2 border-t border-line py-1.5 text-[13px]">
                <span className="w-8 shrink-0 font-mono text-xs text-ink-2">v{h.version}</span>
                <span className="shrink-0">{h.action}</span>
                <span className="min-w-0 truncate text-xs text-ink-2">
                  {h.actor_name} · {stamp(h.at)}
                  {h.run_id ? ` · běh ${h.run_id}` : ""}
                </span>
                {h.version < history.length && (
                  <button
                    type="button"
                    title={t("work.detail.restore", { n: h.version })}
                    aria-label={t("work.detail.restore", { n: h.version })}
                    className="ml-auto shrink-0 text-ink-2 hover:text-accent"
                    onClick={() => onRestore(h.version)}
                  >
                    <RotateCcw size={13} />
                  </button>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}
      {task.assignee_id && task.assignee_type !== "external" && (
        <section>
          <SectionTitle>{t("work.detail.feedback", { name: task.assignee_name })}</SectionTitle>
          <FeedbackForm to={task.assignee_id} taskRef={task.ref} />
        </section>
      )}
    </div>
  );
}

