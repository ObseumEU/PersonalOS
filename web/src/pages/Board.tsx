import { Lock } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { type BoardCard, type BoardRow, agentsApi } from "../agentsApi";
import { ActorChip, StatusDot } from "../components/agents/bits";
import { PageHeader, Panel } from "../components/ui";

const COLUMNS: [keyof Omit<BoardRow, "actor">, string][] = [
  ["queued", "QUEUED"],
  ["working", "WORKING"],
  ["needs_you", "NEEDS YOU"],
  ["done_today", "DONE TODAY"],
];

const GATES: [string, string][] = [
  ["Sign or accept contracts", "people only · agents may prepare"],
  ["Pay or move money", "approval every time"],
  ["Send e-mail or post in your name", "approval"],
  ["Merge to main, deploy", "approval, automatic tests, auto-revert"],
  ["Change permissions, limits, budget", "owner only, never by an agent"],
  ["Turn the kill switch off", "owner only"],
  ["Private topics", "hidden from agents unless shared"],
];

function Card({ c, col }: { c: BoardCard; col: string }) {
  const to = c.approval_id ? "/approvals" : `/tasks?view=agents&task=${c.ref}`;
  return (
    <Link
      to={to}
      className={`block rounded-[3px] border bg-bg px-2 py-1.5 text-xs leading-snug hover:border-accent ${
        col === "needs_you" ? "border-amber-400/60" : "border-line"
      } ${col === "done_today" ? "text-ink-3" : ""}`}
    >
      {c.ref && <span className="cap mr-1">{c.ref}</span>}
      {c.title}
      {col === "working" && c.progress != null && (
        <span className="mt-1 block h-[3px] bg-line">
          <span className="block h-[3px] bg-accent" style={{ width: `${c.progress}%` }} />
        </span>
      )}
    </Link>
  );
}

export default function Board() {
  const [rows, setRows] = useState<BoardRow[] | null>(null);
  useEffect(() => {
    const load = () => agentsApi.board().then(setRows);
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="TASKS · ALL MEMBERS"
        title="Work board"
        sub="Same tasks, one board: what each person and agent has queued, is doing, needs from you, and finished today."
      />
      <div className="flex gap-2">
        <Link to="/network" className="btn">
          Agent network →
        </Link>
        <Link to="/approvals" className="btn">
          Approvals →
        </Link>
      </div>
      <div className="grid gap-4 xl:grid-cols-12">
        <Panel fig="FIG. 7" title="Work across all members" right="live · every 10 s" className="xl:col-span-9" bodyClassName="overflow-x-auto">
          <div className="grid min-w-[760px] grid-cols-[200px_repeat(4,minmax(0,1fr))]">
            <div className="border-b border-line" />
            {COLUMNS.map(([k, label]) => (
              <div key={k} className="border-b border-line px-3 py-2.5">
                <span className={`cap ${k === "needs_you" ? "text-amber-300!" : ""}`}>{label}</span>
              </div>
            ))}
            {rows?.map((r) => (
              <div key={r.actor.id} className="contents">
                <Link to={r.actor.kind === "human" ? "/tasks" : `/agents/${r.actor.id}`} className="flex flex-col gap-1.5 border-b border-line px-3 py-2.5 hover:bg-raised">
                  <ActorChip a={r.actor} />
                  <StatusDot status={r.actor.status} />
                </Link>
                {COLUMNS.map(([k]) => (
                  <div key={k} className="flex flex-col gap-1.5 border-b border-l border-line px-2.5 py-2">
                    {r[k].map((c, i) => (
                      <Card key={`${c.ref ?? c.approval_id}-${i}`} c={c} col={k} />
                    ))}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </Panel>
        <Panel fig="POLICY" title="Human gates" right="enforced by the constitution" className="xl:col-span-3">
          {GATES.map(([a, b]) => (
            <div key={a} className="flex items-start gap-2.5 border-b border-line px-4 py-2.5 last:border-0">
              <Lock size={13} className="mt-0.5 shrink-0 text-amber-300" />
              <span className="flex flex-col gap-0.5">
                <span className="text-[13px]">{a}</span>
                <span className="cap">{b}</span>
              </span>
            </div>
          ))}
        </Panel>
      </div>
    </div>
  );
}
