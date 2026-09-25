import { Sparkles } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AssigneeChip } from "../components/tasks/bits";
import { PageHeader, Panel } from "../components/ui";
import { type AssigneeType, type Step, type Suggestion, type Task, tasksApi } from "../tasksApi";
import { Capture } from "./Tasks";

const input = "h-8 w-full rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

const RULES: [string, string, string][] = [
  ["Actionable?", "no", "Trash, Someday or Reference"],
  ["More than one step?", "yes", "Project: split into steps"],
  ["Under 2 minutes?", "yes", "Do it now"],
  ["Better done by AI, an agent or a person?", "yes", "Assign it"],
  ["Otherwise", "", "Me: next action with a do date"],
];

function stepType(assignee: string): AssigneeType {
  const a = assignee.toLowerCase();
  if (a === "me") return "human";
  if (a === "ai" || a === "assistant") return "ai";
  if (a.includes("agent") || a === "nexus") return "agent";
  return "external";
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

  const setField = (k: keyof Suggestion, v: unknown) => draft && setDraft({ ...draft, [k]: v });
  const setStep = (i: number, s: Partial<Step>) =>
    draft && setDraft({ ...draft, steps: draft.steps.map((x, j) => (j === i ? { ...x, ...s } : x)) });

  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader
        kicker="TASKS · INBOX · CLARIFY"
        title="Inbox"
        sub="Everything lands here first. Process it to zero; the AI proposes, you decide."
      />
      <div className="flex min-h-0 flex-1 flex-col gap-4 lg:flex-row">
        <Panel fig="TAB. 2" title="Inbox" right={`${items?.length ?? 0} to process`} className="lg:w-80 lg:shrink-0" bodyClassName="overflow-y-auto">
          <div className="border-b border-line p-3">
            <Capture onCaptured={() => load()} />
          </div>
          {items?.map((t, i) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setIndex(i)}
              className={`flex w-full flex-col gap-1 border-b border-line px-3.5 py-2.5 text-left ${
                i === index ? "bg-raised shadow-[inset_2px_0_0_var(--color-accent)]" : "hover:bg-raised/60"
              }`}
            >
              <span className="text-[13px]">{t.title}</span>
              <span className="cap">
                {t.ref} · {t.source} · {new Date(t.created_at).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" })}
                {t.suggestion ? " · suggestion ready" : ""}
              </span>
            </button>
          ))}
          {items?.length === 0 && (
            <p className="cap p-6 text-center">
              Inbox zero. <Link to="/tasks?view=today" className="text-accent!">Go to Today →</Link>
            </p>
          )}
        </Panel>

        <Panel
          fig={items?.length ? `ITEM ${index + 1} OF ${items.length}` : "CLARIFY"}
          title="Clarify"
          right="one item at a time"
          className="min-w-0 flex-1"
          bodyClassName="overflow-y-auto"
        >
          {!item ? (
            <p className="cap p-6">Nothing to clarify.</p>
          ) : (
            <div className="flex flex-col gap-4 p-5">
              <div className="flex flex-col gap-1.5">
                <span className="cap">CAPTURED · {item.source.toUpperCase()}</span>
                <span className="text-xl font-light">“{item.title}”</span>
                {item.notes && <span className="text-sm text-ink-2">{item.notes}</span>}
              </div>

              {!draft ? (
                <button type="button" disabled={busy} onClick={suggest} className="btn-accent self-start">
                  <Sparkles size={14} /> {busy ? "Thinking…" : "Suggest with AI"}
                </button>
              ) : (
                <div className="flex flex-col gap-3 rounded-md border border-accent/70 bg-accent/5 p-4">
                  <span className="flex flex-wrap items-center gap-2">
                    <AssigneeChip type="ai" name="AI" />
                    <span className="cap">
                      {draft.engine === "codex" ? "suggested by Codex" : "rule-based: Codex was not available"} ·{" "}
                      {draft.actionable ? "actionable" : "probably not actionable"}
                      {draft.two_minutes ? " · under 2 minutes" : ""}
                    </span>
                    <button type="button" onClick={suggest} disabled={busy} className="cap ml-auto text-accent!">
                      {busy ? "thinking…" : "suggest again"}
                    </button>
                  </span>
                  <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                    <label className="col-span-2 flex flex-col gap-1">
                      <span className="cap">TITLE</span>
                      <input className={input} value={draft.title} onChange={(e) => setField("title", e.target.value)} />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="cap">TOPIC</span>
                      <input className={input} value={draft.topic ?? ""} onChange={(e) => setField("topic", e.target.value || null)} />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="cap">PRIORITY</span>
                      <select className={input} value={draft.priority ?? ""} onChange={(e) => setField("priority", e.target.value ? Number(e.target.value) : null)}>
                        <option value="">none</option>
                        <option value="1">P1 · Must</option>
                        <option value="2">P2 · Should</option>
                        <option value="3">P3 · Could</option>
                      </select>
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="cap">DO DATE</span>
                      <input type="date" className={input} value={draft.do_date ?? ""} onChange={(e) => setField("do_date", e.target.value || null)} />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="cap">DEADLINE</span>
                      <input type="date" className={input} value={draft.deadline ?? ""} onChange={(e) => setField("deadline", e.target.value || null)} />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="cap">ESTIMATE (MIN)</span>
                      <input type="number" className={input} value={draft.estimate_min ?? ""} onChange={(e) => setField("estimate_min", e.target.value ? Number(e.target.value) : null)} />
                    </label>
                    <label className="flex flex-col gap-1">
                      <span className="cap">ASSIGNEE</span>
                      <input className={input} value={draft.assignee ?? ""} placeholder="me, ai, agent, name" onChange={(e) => setField("assignee", e.target.value || null)} />
                    </label>
                  </div>
                  {draft.steps.length > 0 && (
                    <div className="flex flex-col">
                      <span className="cap pb-1 text-ink-2!">PROPOSED STEPS AND WHO DOES THEM</span>
                      {draft.steps.map((s, i) => (
                        <div key={i} className="grid grid-cols-[minmax(0,1fr)_150px_24px] items-center gap-3 border-t border-line py-2">
                          <span className="flex flex-col gap-0.5">
                            <input
                              className="bg-transparent text-[13px] outline-none"
                              value={s.title}
                              onChange={(e) => setStep(i, { title: e.target.value })}
                              aria-label={`Step ${i + 1}`}
                            />
                            {s.reason && <span className="cap">{s.reason}</span>}
                          </span>
                          <span className="flex items-center gap-1.5">
                            <AssigneeChip type={stepType(s.assignee)} name={s.assignee} />
                          </span>
                          <button
                            type="button"
                            aria-label="Remove step"
                            className="text-ink-3 hover:text-ink"
                            onClick={() => setDraft({ ...draft, steps: draft.steps.filter((_, j) => j !== i) })}
                          >
                            ×
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                  <span className="cap">{draft.rationale}</span>
                </div>
              )}

              <div className="flex flex-wrap gap-2">
                <button type="button" disabled={busy} onClick={accept} className="btn-accent">
                  Accept{draft ? "" : " as is"}
                </button>
                <button type="button" disabled={busy} onClick={() => act("do_now")} className="btn">
                  Done now (&lt; 2 min)
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    const who = window.prompt("Delegate to: ai, an agent name, or a person's name");
                    if (who) act("delegate", { assignee: who });
                  }}
                  className="btn"
                >
                  Delegate…
                </button>
                <button type="button" disabled={busy} onClick={() => act("someday")} className="btn">
                  Someday
                </button>
                <button type="button" disabled={busy} onClick={() => act("reference")} className="btn">
                  Reference (→ note)
                </button>
                <button type="button" disabled={busy} onClick={() => act("trash")} className="btn">
                  Trash
                </button>
              </div>
              {error && <p className="cap text-red-400!">{error}</p>}
            </div>
          )}
        </Panel>

        <Panel fig="TAB. 3" title="How items are clarified" right="GTD" className="lg:w-72 lg:shrink-0">
          {RULES.map(([q, a, r], i) => (
            <div key={q} className="grid grid-cols-[22px_minmax(0,1fr)] gap-2 border-b border-line px-3.5 py-2.5">
              <span className="cap text-accent!">{String(i + 1).padStart(2, "0")}</span>
              <span className="flex flex-col gap-1">
                <span className="text-[13px]">{q}</span>
                <span className={`text-xs ${r === "Assign it" ? "text-accent" : "text-ink-2"}`}>
                  {a && <span className="cap mr-1.5 rounded-sm border border-line px-1">{a}</span>}→ {r}
                </span>
              </span>
            </div>
          ))}
          <p className="cap px-3.5 py-3 leading-relaxed">Trash and Someday only archive; everything can be restored from history.</p>
        </Panel>
      </div>
    </div>
  );
}
