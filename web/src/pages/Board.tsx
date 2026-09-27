import { Lock } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { type BoardCard, type BoardRow, agentsApi } from "../agentsApi";
import { ActorChip, StatusDot } from "../components/agents/bits";
import { PageHeader, Panel } from "../components/ui";
import { t } from "../i18n";

const COLUMNS: (keyof Omit<BoardRow, "actor">)[] = ["queued", "working", "needs_you", "done_today"];

// What only people may do (the constitution): [action, rule].
const GATES: [string, string][] = [1, 2, 3, 4, 5, 6, 7].map((i) => [t(`work.gate.${i}.a`), t(`work.gate.${i}.b`)]);

function Card({ c, col }: { c: BoardCard; col: string }) {
  const to = c.approval_id ? "/approvals" : `/tasks?view=agents&task=${c.ref}`;
  return (
    <Link
      to={to}
      className={`block rounded-[3px] border bg-bg px-2 py-1.5 text-xs leading-snug break-words hover:border-accent ${
        col === "needs_you" ? "border-amber-400/60" : "border-line"
      } ${col === "done_today" ? "text-ink-2" : ""}`}
    >
      {c.ref && <span className="mr-1 font-mono text-ink-2">{c.ref}</span>}
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
    const h = setInterval(load, 10000);
    return () => clearInterval(h);
  }, []);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={t("work.board.kicker")} title={t("work.board.title")} sub={t("work.board.sub")} />
      <div className="flex flex-wrap gap-2">
        <Link to="/team?tab=network" className="btn">
          {t("work.board.network")}
        </Link>
        <Link to="/approvals" className="btn">
          {t("work.board.approvals")}
        </Link>
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        <Panel title={t("work.board.panel")} right={t("work.board.live")} className="min-w-0 xl:col-span-9" bodyClassName="overflow-x-auto">
          <div className="grid min-w-[760px] grid-cols-[200px_repeat(4,minmax(0,1fr))]">
            <div className="border-b border-line" />
            {COLUMNS.map((k) => (
              <div key={k} className="border-b border-line px-3 py-2.5">
                <span className={`text-xs font-medium ${k === "needs_you" ? "text-amber-300" : "text-ink-2"}`}>{t(`work.board.col.${k}`)}</span>
              </div>
            ))}
            {rows?.map((r) => (
              <div key={r.actor.id} className="contents">
                <Link to={r.actor.kind === "human" ? "/tasks" : `/team/${r.actor.id}`} className="flex min-w-0 flex-col gap-1.5 border-b border-line px-3 py-2.5 hover:bg-raised">
                  <ActorChip a={r.actor} />
                  <StatusDot status={r.actor.status} />
                </Link>
                {COLUMNS.map((k) => (
                  <div key={k} className="flex min-w-0 flex-col gap-1.5 border-b border-l border-line px-2.5 py-2">
                    {r[k].map((c, i) => (
                      <Card key={`${c.ref ?? c.approval_id}-${i}`} c={c} col={k} />
                    ))}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </Panel>
        <Panel title={t("work.board.gates")} right={t("work.board.gates_right")} className="min-w-0 xl:col-span-3">
          {GATES.map(([a, b]) => (
            <div key={a} className="flex items-start gap-2.5 border-b border-line px-4 py-2.5 last:border-0">
              <Lock size={13} className="mt-0.5 shrink-0 text-amber-300" />
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="text-[13px]">{a}</span>
                <span className="text-xs text-ink-2">{b}</span>
              </span>
            </div>
          ))}
        </Panel>
      </div>
    </div>
  );
}