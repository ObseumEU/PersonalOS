import { Sparkles } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Markdown from "../components/Markdown";
import { confirmDialog } from "../components/overlay";
import { AssigneeChip } from "../components/tasks/bits";
import { PageHeader, Panel } from "../components/ui";
import { LOCALE, t } from "../i18n";
import { type AssigneeType, PRIORITY_LABEL, type Step, type Suggestion, type Task, tasksApi } from "../tasksApi";
import { Capture } from "./Tasks";

const input = "h-8 w-full min-w-0 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

// The GTD questions: [question, answer that leads to the result, result]. The 4th rule is the one about assigning.
const RULES: [string, string, string][] = [
  [t("work.rule.1.q"), t("work.rule.no"), t("work.rule.1.r")],
  [t("work.rule.2.q"), t("work.rule.yes"), t("work.rule.2.r")],
  [t("work.rule.3.q"), t("work.rule.yes"), t("work.rule.3.r")],
  [t("work.rule.4.q"), t("work.rule.yes"), t("work.rule.4.r")],
  [t("work.rule.5.q"), "", t("work.rule.5.r")],
];
const ASSIGN_RULE = 3;

function stepType(assignee: string): AssigneeType {
  const a = assignee.toLowerCase();
  if (a === "me") return "human";
  if (a === "ai" || a === "assistant") return "ai";
  if (a.includes("agent") || a === "nexus") return "agent";
  return "external";
}

function Label({ children }: { children: string }) {
  return <span className="text-xs text-ink-2">{children}</span>;
}

export default function InboxClarify() {
  const [items, setItems] = useState<Task[] | null>(null);
  const [index, setIndex] = useState(0);
  const [draft, setDraft] = useState<Suggestion | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const list = await tasksApi.list("inbox");
    setItems(list);
    setIndex((i) => Math.min(i, Math.max(0, list.length - 1)));
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const item = items?.[index] ?? null;
  useEffect(() => {
    setDraft(item?.suggestion ?? null);
    setError(null);
  }, [item?.id, item?.suggestion]);

  async function suggest() {
    if (!item) return;
    setBusy(true);
    try {
      setDraft(await tasksApi.suggest(item.ref));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function act(action: string, fields?: Record<string, unknown>) {
    if (!item) return;
    setBusy(true);
    try {
      await tasksApi.clarify(item.ref, action, fields);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function accept() {
    if (!draft) return act("accept", {});
    const { engine: _e, actionable: _a, two_minutes: _t, rationale: _r, steps, assignee, ...fields } = draft;
    const clean = Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== null && v !== ""));
    act("accept", { ...clean, ...(assignee ? { assignee } : {}), steps });
  }

  async function delegate() {
    const who = await confirmDialog({
      title: t("work.clarify.delegate_title"),
      body: t("work.clarify.delegate_body"),
      confirm: t("work.clarify.delegate_confirm"),
      reason: t("work.clarify.delegate_reason"),
    });
    if (who) act("delegate", { assignee: who });
  }

  const setField = (k: keyof Suggestion, v: unknown) => draft && setDraft({ ...draft, [k]: v });
  const setStep = (i: number, s: Partial<Step>) =>
    draft && setDraft({ ...draft, steps: draft.steps.map((x, j) => (j === i ? { ...x, ...s } : x)) });

  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader kicker={t("work.clarify.kicker")} title={t("work.view.inbox")} sub={t("work.clarify.sub")} />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 lg:flex-row">
        <Panel
          title={t("work.view.inbox")}
          right={t("work.clarify.to_process", { n: items?.length ?? 0 })}
          className="min-w-0 lg:w-80 lg:shrink-0"
          bodyClassName="overflow-y-auto"
        >
          <div className="border-b border-line p-3">
            <Capture onCaptured={() => load()} />
          </div>
          {items?.map((task, i) => (
            <button
              key={task.id}
              type="button"
              onClick={() => setIndex(i)}
              className={`flex w-full min-w-0 flex-col gap-1 border-b border-line px-3.5 py-2.5 text-left ${
                i === index ? "bg-raised shadow-[inset_2px_0_0_var(--color-accent)]" : "hover:bg-raised/60"
              }`}
            >
              <span className="text-[13px] break-words">{task.title}</span>
              <span className="text-xs break-words text-ink-2">
                {task.ref} · {task.source} · {new Date(task.created_at).toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" })}
                {task.suggestion ? ` · ${t("work.clarify.suggestion_ready")}` : ""}
              </span>
            </button>
          ))}
          {items?.length === 0 && (
            <p className="p-6 text-center text-sm text-ink-2">
              {t("work.tasks.empty.inbox")}{" "}
              <Link to="/tasks?view=today" className="text-accent">
                {t("work.clarify.go_today")}
              </Link>
            </p>
          )}
        </Panel>

        <Panel
          title={t("work.clarify.title")}
          right={t("work.clarify.one_at_a_time")}
          className="min-w-0 flex-1"
          bodyClassName="overflow-y-auto"
        >
          {!item ? (
            <p className="p-6 text-sm text-ink-2">{t("work.clarify.nothing")}</p>
          ) : (
            <div className="flex min-w-0 flex-col gap-4 p-4 sm:p-5">
              <div className="flex min-w-0 flex-col gap-1.5">
                <span className="text-xs text-ink-2">{t("work.clarify.captured", { source: item.source })}</span>
                <span className="text-xl font-light break-words">„{item.title}“</span>
                {item.notes && <Markdown text={item.notes} compact className="md-muted" />}
              </div>

              {!draft ? (
                <button type="button" disabled={busy} onClick={suggest} className="btn-accent self-start">
                  <Sparkles size={14} /> {busy ? t("work.clarify.thinking") : t("work.clarify.suggest")}
                </button>
              ) : (
                <div className="flex min-w-0 flex-col gap-3 rounded-md border border-accent/70 bg-accent/5 p-3 sm:p-4">
                  <span className="flex flex-wrap items-center gap-2">
                    <AssigneeChip type="ai" name="AI" />
                    <span className="text-xs text-ink-2">
                      {draft.engine === "codex" ? t("work.clarify.by_codex") : t("work.clarify.by_rules")} ·{" "}
                      {draft.actionable ? t("work.clarify.actionable") : t("work.clarify.not_actionable")}
                      {draft.two_minutes ? ` · ${t("work.clarify.two_minutes")}` : ""}
                    </span>
                    <button type="button" onClick={suggest} disabled={busy} className="ml-auto text-xs text-accent">
                      {busy ? t("work.clarify.thinking") : t("work.clarify.again")}
                    </button>
                  </span>
                  <div className="grid grid-cols-1 gap-3 min-[400px]:grid-cols-2 md:grid-cols-4">
                    <label className="flex min-w-0 flex-col gap-1 min-[400px]:col-span-2">
                      <Label>{t("work.clarify.f.title")}</Label>
                      <input className={input} value={draft.title} onChange={(e) => setField("title", e.target.value)} />
                    </label>
                    <label className="flex min-w-0 flex-col gap-1">
                      <Label>{t("work.detail.topic")}</Label>
                      <input className={input} value={draft.topic ?? ""} onChange={(e) => setField("topic", e.target.value || null)} />
                    </label>
                    <label className="flex min-w-0 flex-col gap-1">
                      <Label>{t("priority.label")}</Label>
                      <select className={input} value={draft.priority ?? ""} onChange={(e) => setField("priority", e.target.value ? Number(e.target.value) : null)}>
                        <option value="">{t("work.priority.unset")}</option>
                        <option value="1">P1 · {PRIORITY_LABEL[1]}</option>
                        <option value="2">P2 · {PRIORITY_LABEL[2]}</option>
                        <option value="3">P3 · {PRIORITY_LABEL[3]}</option>
                      </select>
                    </label>
                    <label className="flex min-w-0 flex-col gap-1">
                      <Label>{t("work.detail.do_date")}</Label>
                      <input type="date" className={input} value={draft.do_date ?? ""} onChange={(e) => setField("do_date", e.target.value || null)} />
                    </label>
                    <label className="flex min-w-0 flex-col gap-1">
                      <Label>{t("work.detail.deadline")}</Label>
                      <input type="date" className={input} value={draft.deadline ?? ""} onChange={(e) => setField("deadline", e.target.value || null)} />
                    </label>
                    <label className="flex min-w-0 flex-col gap-1">
                      <Label>{t("work.detail.estimate")}</Label>
                      <input type="number" className={input} value={draft.estimate_min ?? ""} onChange={(e) => setField("estimate_min", e.target.value ? Number(e.target.value) : null)} />
                    </label>
                    <label className="flex min-w-0 flex-col gap-1">
                      <Label>{t("work.detail.assignee")}</Label>
                      <input
                        className={input}
                        value={draft.assignee ?? ""}
                        placeholder={t("work.clarify.f.assignee_ph")}
                        onChange={(e) => setField("assignee", e.target.value || null)}
                      />
                    </label>
                  </div>
                  {draft.steps.length > 0 && (
                    <div className="flex min-w-0 flex-col">
                      <span className="pb-1 text-xs text-ink-2">{t("work.clarify.steps")}</span>
                      {draft.steps.map((s, i) => (
                        <div key={i} className="grid grid-cols-[minmax(0,1fr)_auto_24px] items-center gap-2 border-t border-line py-2 sm:gap-3">
                          <span className="flex min-w-0 flex-col gap-0.5">
                            <input
                              className="min-w-0 bg-transparent text-[13px] outline-none"
                              value={s.title}
                              onChange={(e) => setStep(i, { title: e.target.value })}
                              aria-label={t("work.clarify.step_aria", { n: i + 1 })}
                            />
                            {s.reason && <span className="text-xs break-words text-ink-2">{s.reason}</span>}
                          </span>
                          <span className="flex max-w-[140px] min-w-0 items-center gap-1.5">
                            <AssigneeChip type={stepType(s.assignee)} name={s.assignee} />
                          </span>
                          <button
                            type="button"
                            aria-label={t("work.clarify.remove_step")}
                            className="text-ink-2 hover:text-ink"
                            onClick={() => setDraft({ ...draft, steps: draft.steps.filter((_, j) => j !== i) })}
                          >
                            ×
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                  <span className="text-xs break-words text-ink-2">{draft.rationale}</span>
                </div>
              )}

              <div className="flex flex-wrap gap-2">
                <button type="button" disabled={busy} onClick={accept} className="btn-accent">
                  {draft ? t("work.clarify.accept") : t("work.clarify.accept_as_is")}
                </button>
                <button type="button" disabled={busy} onClick={() => act("do_now")} className="btn">
                  {t("work.clarify.do_now")}
                </button>
                <button type="button" disabled={busy} onClick={delegate} className="btn">
                  {t("work.clarify.delegate")}
                </button>
                <button type="button" disabled={busy} onClick={() => act("someday")} className="btn">
                  {t("work.clarify.someday")}
                </button>
                <button type="button" disabled={busy} onClick={() => act("reference")} className="btn">
                  {t("work.clarify.reference")}
                </button>
                <button type="button" disabled={busy} onClick={() => act("trash")} className="btn">
                  {t("work.clarify.trash")}
                </button>
              </div>
              {error && <p className="text-xs break-words text-red-400">{error}</p>}
            </div>
          )}
        </Panel>

        <Panel title={t("work.clarify.rules_title")} right="GTD" className="min-w-0 lg:w-72 lg:shrink-0">
          {RULES.map(([q, a, r], i) => (
            <div key={q} className="grid grid-cols-[22px_minmax(0,1fr)] gap-2 border-b border-line px-3.5 py-2.5">
              <span className="text-xs text-accent tabular-nums">{i + 1}</span>
              <span className="flex min-w-0 flex-col gap-1">
                <span className="text-[13px]">{q}</span>
                <span className={`text-xs ${i === ASSIGN_RULE ? "text-accent" : "text-ink-2"}`}>
                  {a && <span className="mr-1.5 rounded-sm border border-line px-1">{a}</span>}→ {r}
                </span>
              </span>
            </div>
          ))}
          <p className="px-3.5 py-3 text-xs leading-relaxed text-ink-2">{t("work.clarify.rules_footer")}</p>
        </Panel>
      </div>
    </div>
  );
}
