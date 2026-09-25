import { useState } from "react";
import { Link } from "react-router-dom";
import { type Answer, knowledgeApi } from "../knowledgeApi";
import Markdown from "./Markdown";
import { AskBox } from "./ui";

/** Today's quick question to knowlage: the answer with its cited sources, right here.
 *  A conversation (follow-ups, tasks) is a DM with the Assistant. */
export default function QuickAnswer() {
  const [q, setQ] = useState<string | null>(null);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const busy = q !== null && answer === null;
  return (
    <div className="flex flex-col gap-2">
      <AskBox
        id="ask-today"
        placeholder="Ask our knowledge base (answers cite their sources)"
        busy={busy}
        onAsk={(question) => {
          setQ(question);
          setAnswer(null);
          knowledgeApi.ask(question).then(setAnswer, (e) => setAnswer({ ok: false, error: e.message }));
        }}
      />
      {busy && <p className="cap breathe">Researching in knowlage… reading sources and checking every quote.</p>}
      {answer && !answer.ok && <p className="cap text-red-400!">{answer.error}</p>}
      {answer?.ok && (
        <div className="panel flex max-h-[320px] flex-col gap-2 overflow-y-auto p-4 text-[13px]">
          <Markdown text={answer.answer ?? ""} />
          {!!answer.citations?.length && (
            <ol className="flex flex-col gap-1 border-t border-line pt-2">
              {answer.citations.map((c) => (
                <li key={c.n} className="cap">
                  [{c.n}] {c.passages.map((p) => p.title).join(" · ")} {c.verified ? "· verified" : ""}
                </li>
              ))}
            </ol>
          )}
          <Link to={`/assistant?q=${encodeURIComponent(q ?? "")}`} className="cap hover:text-accent">
            continue with the Assistant in chat →
          </Link>
        </div>
      )}
    </div>
  );
}
