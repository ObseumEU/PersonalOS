import KnowledgeGraph from "../components/LazyGraph";
import { AskBox, PageHeader, Panel, SampleBadge } from "../components/ui";
import { ANSWER, GRAPH_LABELS } from "../sample";

const SOURCES = [0, 6, 12, 18];

export default function Assistant() {
  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader
        kicker="ASSISTANT · CODEX CLI · MCP + A2A"
        title="Answers, with their sources."
        sub={
          <span className="flex flex-wrap items-center gap-2">
            The assistant arrives in Phase 3. This is an example session. <SampleBadge />
          </span>
        }
      />
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel fig="SESSION" title="Example" right="codex · 4 sources · 2.8 s" className="lg:col-span-7" bodyClassName="flex flex-col">
          <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-6 py-5">
            <div className="flex flex-col gap-1.5">
              <span className="cap">QUERY</span>
              <span className="text-xl font-light tracking-[-0.01em]">{ANSWER.query}</span>
            </div>
            <div className="flex flex-col gap-1">
              <span className="cap">ANSWER</span>
              <p className="mt-1 mb-1.5 text-[15px] leading-relaxed text-ink-2">{ANSWER.summary}</p>
              <ol>
                {ANSWER.items.map((it, i) => (
                  <li
                    key={it.ref}
                    className="fade-in grid grid-cols-[28px_minmax(0,1fr)] gap-2 border-t border-line py-3"
                    style={{ animationDelay: `${0.2 + i * 0.2}s` }}
                  >
                    <span className="cap pt-0.5 text-accent!">{String(i + 1).padStart(2, "0")}</span>
                    <span className="text-[15px] leading-relaxed">
                      {it.text}
                      <sup className="text-accent"> [{it.ref}]</sup>
                    </span>
                  </li>
                ))}
              </ol>
            </div>
            <div className="mt-auto border-t border-line pt-3">
              <span className="cap text-ink!">REFERENCES</span>
              {ANSWER.refs.map((r, i) => (
                <span key={r} className="cap block py-0.5 text-ink-2!">
                  [{i + 1}] {r}
                </span>
              ))}
            </div>
          </div>
          <div className="px-4 pb-4">
            <AskBox id="ask-assistant" placeholder="Ask a follow-up, or “research deeper” to use the Knowledge agent" />
          </div>
        </Panel>

        <div className="flex min-h-0 flex-col gap-4 lg:col-span-5">
          <Panel
            fig="FIG. 3"
            title="Sources in the knowledge graph"
            right="4 of 38 nodes"
            className="h-[320px] lg:h-auto lg:flex-[1.2]"
            bodyClassName="measure-grid relative"
          >
            <KnowledgeGraph nodes={38} seed={3} labels={GRAPH_LABELS.slice(0, 6)} highlight={SOURCES} period={110} />
          </Panel>
          <Panel fig="TAB. 2" title="Retrieval trace" right="total 2.78 s">
            {ANSWER.trace.map((t, i) => (
              <div key={t.step} className="grid grid-cols-[26px_minmax(0,1fr)_76px_54px] gap-2.5 border-b border-line px-4 py-2 last:border-0">
                <span className="cap">{i + 1}</span>
                <span className="text-[13px]">{t.step}</span>
                <span className="cap">{t.via}</span>
                <span className="cap text-right text-ink-2!">{t.time}</span>
              </div>
            ))}
          </Panel>
        </div>
      </div>
    </div>
  );
}
