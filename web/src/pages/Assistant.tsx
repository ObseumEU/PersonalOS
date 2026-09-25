import { CheckCircle2, CircleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import KnowledgePanel from "../components/KnowledgePanel";
import { AskBox, PageHeader, Panel } from "../components/ui";
import { type Answer, knowledgeApi, useKnowledgeGraph } from "../knowledgeApi";

type Turn = { question: string; answer: Answer | null; started: number; took?: number };

/** Answer text with [n] markers turned into accent superscripts. */
function Marked({ text }: { text: string }) {
  const parts = text.split(/(\[\d+\])/g);
  return (
    <>
      {parts.map((p, i) =>
        /^\[\d+\]$/.test(p) ? (
          <sup key={i} className="text-accent">
            {p}
          </sup>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

export default function Assistant() {
  const [params, setParams] = useSearchParams();
  const [turns, setTurns] = useState<Turn[]>([]);
  const graph = useKnowledgeGraph();
  const asked = useRef<string | null>(null);
  const busy = turns.some((t) => t.answer === null);

  const ask = (question: string) => {
    const started = Date.now();
    setTurns((ts) => [...ts, { question, answer: null, started }]);
    knowledgeApi.ask(question).then(
      (answer) => setTurns((ts) => ts.map((t) => (t.started === started ? { ...t, answer, took: Date.now() - started } : t))),
      (e) => setTurns((ts) => ts.map((t) => (t.started === started ? { ...t, answer: { ok: false, error: e.message } } : t))),
    );
  };

  // A question handed over from the Ask box on another page.
  useEffect(() => {
    const q = params.get("q");
    if (q && asked.current !== q) {
      asked.current = q;
      ask(q);
      setParams({}, { replace: true });
    }
  }, [params]); // eslint-disable-line react-hooks/exhaustive-deps

  const last = [...turns].reverse().find((t) => t.answer?.ok);
  // Documents the last answer cites, when they are in the graph.
  const cited = useMemo(() => {
    const ids = new Set((last?.answer?.citations ?? []).flatMap((c) => c.passages.map((p) => `doc:${p.doc_id}`)));
    return graph?.nodes.filter((n) => ids.has(n.id)).map((n) => n.id) ?? [];
  }, [last, graph]);

  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader
        kicker="ASSISTANT · KNOWLAGE · VERIFIED CITATIONS"
        title="Answers, with their sources."
        sub="Questions go to our knowledge base (knowlage). Every claim cites the passage it comes from, and each quote is checked against the source."
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel
          fig="SESSION"
          title={turns.length ? `${turns.length} question${turns.length > 1 ? "s" : ""}` : "New session"}
          right={last?.took ? `knowlage · ${last.answer?.citations?.length ?? 0} citations · ${(last.took / 1000).toFixed(1)} s` : "knowlage"}
          className="lg:col-span-7"
          bodyClassName="flex flex-col"
        >
          <div className="flex min-h-[240px] flex-1 flex-col gap-6 overflow-y-auto px-6 py-5">
            {turns.length === 0 && (
              <p className="cap m-auto max-w-md text-center leading-relaxed">
                Ask about anything in the knowledge base: the Alex Hormozi library in the default workspace, or the company's GitHub
                repositories in "Celá firma". Research takes a minute or two.
              </p>
            )}
            {turns.map((t) => (
              <div key={t.started} className="flex flex-col gap-3">
                <div className="flex flex-col gap-1.5">
                  <span className="cap">QUERY</span>
                  <span className="text-xl font-light tracking-[-0.01em]">{t.question}</span>
                </div>
                {t.answer === null && <p className="cap breathe">Researching in knowlage… reading sources and checking every quote.</p>}
                {t.answer && !t.answer.ok && (
                  <p className="flex items-start gap-2 text-sm text-red-400">
                    <CircleAlert size={15} className="mt-0.5 shrink-0" /> {t.answer.error}
                  </p>
                )}
                {t.answer?.ok && (
                  <>
                    <div className="flex flex-col gap-1">
                      <span className="cap flex items-center gap-2">
                        ANSWER
                        {t.answer.verified ? (
                          <span className="flex items-center gap-1 text-accent!">
                            <CheckCircle2 size={12} /> all quotes verified
                          </span>
                        ) : (
                          <span className="text-ink-2!">some claims not verified</span>
                        )}
                      </span>
                      <div className="mt-1 whitespace-pre-wrap text-[15px] leading-relaxed text-ink-2">
                        <Marked text={t.answer.answer ?? ""} />
                      </div>
                    </div>
                    {!!t.answer.citations?.length && (
                      <div className="border-t border-line pt-3">
                        <span className="cap text-ink!">REFERENCES</span>
                        {t.answer.citations.map((c) => (
                          <div key={c.n} className="flex flex-col gap-0.5 py-1">
                            {c.passages.slice(0, 2).map((p, i) => (
                              <span key={i} className="cap block text-ink-2!">
                                [{c.n}]{" "}
                                {p.url ? (
                                  <a href={p.url} target="_blank" rel="noreferrer" className="hover:text-accent!">
                                    {p.title}
                                  </a>
                                ) : (
                                  p.title
                                )}
                                {p.location ? ` · ${p.location}` : ""}
                                {c.verified ? "" : " · unverified"}
                              </span>
                            ))}
                          </div>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
          <div className="px-4 pb-4">
            <AskBox id="ask-assistant" placeholder={turns.length ? "Ask another question" : "Ask the knowledge base"} onAsk={ask} busy={busy} />
          </div>
        </Panel>

        <div className="flex min-h-0 flex-col gap-4 lg:col-span-5">
          <KnowledgePanel
            fig="FIG. 3"
            title="Sources in the knowledge graph"
            className="h-[320px] lg:h-auto lg:flex-[1.2]"
            highlight={cited}
            period={110}
            graph={graph}
          />
          <Panel fig="TAB. 2" title="Cited passages" right={last ? `${last.answer?.steps ?? 0} research steps` : "—"} bodyClassName="max-h-[260px] overflow-y-auto">
            {!last && <p className="cap px-4 py-3">The passages behind the last answer appear here.</p>}
            {last?.answer?.citations?.flatMap((c) =>
              c.passages.slice(0, 1).map((p, i) => (
                <div key={`${c.n}-${i}`} className="grid grid-cols-[26px_minmax(0,1fr)] gap-2.5 border-b border-line px-4 py-2 last:border-0">
                  <span className="cap text-accent!">{c.n}</span>
                  <span className="flex flex-col gap-0.5">
                    <span className="text-[13px] leading-snug">“{p.quote}”</span>
                    <span className="cap truncate">
                      {p.title}
                      {p.location ? ` · ${p.location}` : ""}
                    </span>
                  </span>
                </div>
              )),
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}
