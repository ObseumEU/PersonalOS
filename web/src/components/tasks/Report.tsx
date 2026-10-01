import { Check, ChevronDown, ExternalLink, FileText, Lightbulb, ListChecks, RefreshCw, Sparkles } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ago, t } from "../../i18n";
import { refreshNeedsMe } from "../../needsMeApi";
import { type Decision, type OwnerReport, reportApi, reportHref, type Source } from "../../reportApi";
import { taskChanged } from "../../taskSheet";
import Markdown from "../Markdown";
import { toast } from "../overlay";
import { RefChip } from "../RefPreview";

/*
 * The owner's report on a hand-in, in the order a busy CEO reads it:
 *   Co si z toho odnést  ->  Co od tebe potřebuju (decision cards)  ->  Co se stane dál
 *   Podrobnosti (collapsed): the result itself, sources, changes, verification, what was not found.
 * The same pieces make the full report page (pages/ReportPage.tsx), opened from the panel and pings.
 */

type Available = Extract<OwnerReport, { available: true }>;

export function useReport(taskRef: string, enabled: boolean, version?: string) {
  const [report, setReport] = useState<OwnerReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const load = useCallback(
    (generate = true) => {
      if (!enabled) return;
      setError(null);
      // The cached one at once (quietly), then the built one when it has to be built.
      reportApi
        .get(taskRef, false)
        .then((r) => {
          setReport(r);
          if (!r.available && r.can_build && generate) {
            setLoading(true);
            return reportApi.get(taskRef, true).then(setReport);
          }
        })
        .catch((e) => setError(e instanceof Error ? e.message : String(e)))
        .finally(() => setLoading(false));
    },
    [taskRef, enabled],
  );
  useEffect(() => {
    setReport(null);
    load();
  }, [load, version]);
  return { report, error, loading, setReport, reload: load };
}

function Head({ icon, children, id }: { icon: ReactNode; children: ReactNode; id?: string }) {
  return (
    <h3 id={id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[13px] font-medium tracking-wide text-ink-2 uppercase">
      <span aria-hidden className="text-accent">
        {icon}
      </span>
      {children}
    </h3>
  );
}

function DecisionCard({ d, taskRef, onDone, autoFocus }: { d: Decision; taskRef: string; onDone: (r: OwnerReport) => void; autoFocus?: boolean }) {
  const [choice, setChoice] = useState<string | null>(d.decided?.choice || null);
  const [note, setNote] = useState(d.decided?.note ?? "");
  const [editing, setEditing] = useState(!d.decided);
  const [busy, setBusy] = useState(false);
  const [showContext, setShowContext] = useState(false);
  useEffect(() => {
    setEditing(!d.decided);
    setChoice(d.decided?.choice || null);
    setNote(d.decided?.note ?? "");
  }, [d.decided]);
  const send = async (pick: string | null) => {
    if (!pick && !note.trim()) return;
    setBusy(true);
    try {
      const r = await reportApi.decide(taskRef, d.id, pick ?? "", note.trim());
      toast(r.available && r.resumed ? t("rp.resumed", { who: r.assignee ?? "" }) : t("rp.saved"));
      onDone(r);
      taskChanged();
      refreshNeedsMe();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
    }
  };
  if (d.decided && !editing)
    return (
      <div className="flex flex-col gap-1.5 rounded-lg border border-emerald-400/30 bg-emerald-400/[0.05] p-3.5">
        <p className="text-[15px] leading-snug">{d.question}</p>
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[14px] text-emerald-100">
          <Check size={15} aria-hidden className="text-emerald-300" />
          {t("rp.decided", { choice: [d.decided.choice, d.decided.note].filter(Boolean).join(" — ") })}
          <span className="text-xs text-ink-2">{t("rp.decided_by", { who: d.decided.by ?? "", when: ago(d.decided.at) })}</span>
          <button type="button" className="text-xs text-accent hover:underline print:hidden" onClick={() => setEditing(true)}>
            {t("rp.change")}
          </button>
        </p>
      </div>
    );
  const name = `dec-${d.id}`;
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-line bg-bg/60 p-3.5 sm:p-4">
      <div className="flex flex-col gap-1">
        <p className="text-[16px] leading-snug font-medium text-ink">{d.question}</p>
        {d.why && <p className="text-[13px] leading-relaxed text-ink-2">{t("rp.why", { why: d.why.replace(/\.$/, "") })}</p>}
        {d.context && d.context.length <= 400 && (
          <div className="mt-1 border-l-2 border-accent/40 pl-3 text-[14px] text-ink">
            <span className="text-xs text-ink-2">{t("rp.proposal")}</span>
            <Markdown text={d.context} compact className="text-[14px]" />
          </div>
        )}
      </div>
      <div role="radiogroup" aria-label={d.question} className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
        {d.options.map((o, i) => {
          const rec = o === d.recommendation;
          const on = choice === o;
          return (
            <button
              key={o}
              type="button"
              role="radio"
              aria-checked={on}
              data-needs-focus={autoFocus && i === 0 ? "" : undefined}
              disabled={busy}
              onClick={() => {
                setChoice(o);
                send(o);
              }}
              className={`flex min-h-11 items-center gap-2 rounded-md border px-3 py-2 text-left text-[14px] transition sm:min-w-[180px] sm:flex-1 ${
                on ? "border-accent bg-accent/15 text-ink" : rec ? "border-emerald-400/60 bg-emerald-400/[0.07] hover:border-emerald-300" : "border-line hover:border-ink-3"
              }`}
            >
              <span className="min-w-0 flex-1 break-words">{o}</span>
              {rec && (
                <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-emerald-400/15 px-2 py-0.5 text-xs text-emerald-200">
                  <Sparkles size={12} aria-hidden /> {t("rp.recommended")}
                </span>
              )}
            </button>
          );
        })}
      </div>
      {d.context && d.context.length > 400 && (
        <div className="text-[13px]">
          <button type="button" className="inline-flex items-center gap-1 text-ink-2 hover:text-ink" aria-expanded={showContext} onClick={() => setShowContext(!showContext)}>
            <ChevronDown size={13} className={showContext ? "rotate-180" : ""} /> {t("rp.context")}
          </button>
          {showContext && (
            <div className="mt-1.5 border-l-2 border-line pl-3 text-ink-2">
              <Markdown text={d.context} compact />
            </div>
          )}
        </div>
      )}
      <form
        className="flex flex-col gap-2 sm:flex-row sm:items-start"
        onSubmit={(e) => {
          e.preventDefault();
          send(choice);
        }}
      >
        <label htmlFor={`${name}-note`} className="sr-only">
          {t("rp.your_answer")}
        </label>
        <textarea
          id={`${name}-note`}
          rows={1}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={t("rp.answer_placeholder")}
          className="min-h-10 w-full rounded-md border border-line bg-bg px-3 py-2 text-sm outline-none focus:border-accent sm:flex-1"
        />
        <button type="submit" className="btn h-10! shrink-0" disabled={busy || !note.trim()}>
          {t("rp.send_text")}
        </button>
      </form>
    </div>
  );
}

/** Takeaway, decisions, next: the top of the report. */
export function ReportTop({ r, onChange, autoFocus }: { r: Available; onChange: (r: OwnerReport) => void; autoFocus?: boolean }) {
  return (
    <div className="flex flex-col gap-6">
      <section aria-labelledby="rp-takeaway" className="flex flex-col gap-2">
        <Head id="rp-takeaway" icon={<Lightbulb size={15} />}>
          {t("rp.takeaway")}
        </Head>
        <p className="text-[17px] leading-relaxed text-ink sm:text-[18px]">{r.takeaway}</p>
      </section>
      <section aria-labelledby="rp-needs" className="flex flex-col gap-3">
        <Head id="rp-needs" icon={<Check size={15} />}>
          {t("rp.needs")}
          {r.decisions.length > 0 && (
            <span className="ml-1 font-normal tracking-normal normal-case">
              · {r.decisions_open ? t("rp.open_count", { n: r.decisions_open }) : t("rp.all_decided")}
            </span>
          )}
        </Head>
        {r.decisions.length === 0 ? (
          <p className="text-[15px] text-ink-2">{t("rp.needs_none")}</p>
        ) : (
          r.decisions.map((d, i) => <DecisionCard key={d.id} d={d} taskRef={r.task} onDone={onChange} autoFocus={autoFocus && i === 0} />)
        )}
      </section>
      {r.next && (
        <section aria-labelledby="rp-next" className="flex flex-col gap-1.5">
          <Head id="rp-next" icon={<ListChecks size={15} />}>
            {t("rp.next")}
          </Head>
          <p className="text-[15px] leading-relaxed">{r.next}</p>
        </section>
      )}
    </div>
  );
}

function SourceItem({ s }: { s: Source }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="flex flex-col gap-1 border-b border-line py-2 last:border-0">
      <span className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-[14px] text-ink">{s.title}</span>
        {s.quote && (
          <button type="button" className="text-xs text-accent hover:underline print:hidden" aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? t("rp.quote_hide") : t("rp.quote_show")}
          </button>
        )}
        {s.link && /^https?:/.test(s.link) && (
          <a href={s.link} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-xs text-ink-2 hover:text-accent">
            <ExternalLink size={11} /> {t("rp.open_source")}
          </a>
        )}
      </span>
      {s.quote && <blockquote className={`border-l-2 border-accent/40 pl-3 text-[13px] leading-relaxed whitespace-pre-wrap text-ink-2 ${open ? "" : "hidden print:block"}`}>{s.quote}</blockquote>}
    </li>
  );
}

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h4 className="text-xs font-medium tracking-wide text-ink-2 uppercase">{title}</h4>
      {children}
    </section>
  );
}

/** The details: the result itself (inline), changes, verification, sources, related, unresolved. */
export function ReportDetails({ r, full = false }: { r: Available; full?: boolean }) {
  const d = r.details;
  const list = (xs: string[]) => (
    <ul className="flex list-disc flex-col gap-1 pl-5 text-[14px] marker:text-ink-3">
      {xs.map((x) => (
        <li key={x}>
          <Markdown text={x} compact className="text-[14px]" />
        </li>
      ))}
    </ul>
  );
  return (
    <div className="flex flex-col gap-6">
      {d.summary.length > 0 && <Block title={t("rp.summary")}>{list(d.summary)}</Block>}
      {d.content && (
        <Block title={d.content_title ? `${t("rp.content")} · ${d.content_title}` : t("rp.content")}>
          <Markdown text={d.content} className={full ? "text-[15px] leading-relaxed" : "text-[14px]"} />
        </Block>
      )}
      {d.changes.length > 0 && <Block title={t("rp.changes")}>{list(d.changes)}</Block>}
      {d.verification.length > 0 && <Block title={t("rp.verification")}>{list(d.verification)}</Block>}
      {d.sources.length > 0 && (
        <Block title={t("rp.sources")}>
          <ul className="flex flex-col">
            {d.sources.map((s) => (
              <SourceItem key={`${s.ref ?? s.title}-${s.quote.slice(0, 20)}`} s={s} />
            ))}
          </ul>
        </Block>
      )}
      {d.related.length > 0 && (
        <Block title={t("rp.related")}>
          <ul className="flex flex-wrap gap-2">
            {d.related.map((x) => (
              <li key={`${x.kind}-${x.id}`}>
                <RefChip kind={x.kind} id={String(x.id)} label={x.kind === "task" ? String(x.id) : undefined} />
              </li>
            ))}
          </ul>
        </Block>
      )}
      {d.unresolved.length > 0 && (
        <Block title={t("rp.unresolved")}>
          <p className="text-[13px] text-ink-2">{t("rp.unresolved_hint")}</p>
          <ul className="flex flex-col gap-1 text-[13px]">
            {d.unresolved.map((u) => (
              <li key={`${u.kind}-${u.id}`} className="flex flex-wrap gap-x-2">
                <span className="font-mono text-ink-2">{u.raw ?? u.id}</span>
                <span className="text-amber-200/90">{u.why}</span>
              </li>
            ))}
          </ul>
        </Block>
      )}
      {d.original && (
        <details className="rounded-lg border border-line px-3 py-2 print:hidden">
          <summary className="cursor-pointer text-[13px] text-ink-2">{t("rp.original")}</summary>
          <div className="pt-2">
            <Markdown text={d.original} className="text-[13px]" />
          </div>
        </details>
      )}
      <p className="text-xs text-ink-3">
        {r.source === "agent" ? t("rp.by_agent", { who: r.author ?? "" }) : t("rp.by_builder")}
        {r.takeaway_source === "fallback" ? ` ${t("rp.fallback_note")}` : ""}
        {r.stale ? ` ${t("rp.stale")}` : ""}
      </p>
    </div>
  );
}

/** In the task panel: the top open, the details folded, and a link to the full report page. */
export default function ReportCard({ taskRef, version, focus }: { taskRef: string; version?: string; focus?: boolean }) {
  const { report, error, loading, setReport } = useReport(taskRef, true, version);
  const [open, setOpen] = useState(false);
  if (!report || !report.available) {
    if (loading)
      return (
        <section className="flex flex-col gap-2 rounded-xl border border-accent/30 bg-accent/[0.04] p-4 sm:p-5" aria-busy="true">
          <p className="flex items-center gap-2 text-[15px]">
            <RefreshCw size={15} className="animate-spin text-accent" aria-hidden /> {t("rp.loading")}
          </p>
          <p className="text-[13px] text-ink-2">{t("rp.loading_hint")}</p>
        </section>
      );
    if (error) return <p className="text-sm text-amber-200">{t("rp.error", { error })}</p>;
    return null;
  }
  return (
    <section id="owner-report" aria-label={t("rp.takeaway")} className="flex flex-col gap-5 rounded-xl border border-accent/30 bg-accent/[0.03] p-4 sm:p-6">
      <ReportTop r={report} onChange={setReport} autoFocus={focus} />
      <div className="flex flex-col gap-3 border-t border-line pt-4">
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn h-9!" aria-expanded={open} aria-controls="rp-details" onClick={() => setOpen(!open)}>
            <ChevronDown size={14} className={open ? "rotate-180" : ""} /> {t("rp.details")}
            <span className="text-xs text-ink-2">· {t("rp.details_hint")}</span>
          </button>
          <Link to={reportHref(report.task)} className="btn-accent h-9!">
            <FileText size={14} /> {t("rp.open_full")}
          </Link>
          {report.source === "builder" && (
            <button
              type="button"
              className="ml-auto inline-flex items-center gap-1 text-xs text-ink-2 hover:text-accent"
              onClick={() =>
                reportApi.rebuild(report.task).then(
                  (r) => {
                    setReport(r);
                    toast(t("rp.rebuilt"));
                  },
                  (e) => toast(e.message, { error: true }),
                )
              }
            >
              <RefreshCw size={12} /> {t("rp.rebuild")}
            </button>
          )}
        </div>
        {open && (
          <div id="rp-details" className="pt-2">
            <ReportDetails r={report} />
          </div>
        )}
      </div>
      {loading && <span className="sr-only">{t("rp.loading")}</span>}
    </section>
  );
}
