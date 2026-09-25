import { Check, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { type Approval, agentsApi } from "../agentsApi";
import { ActorChip } from "../components/agents/bits";
import { PageHeader, Panel } from "../components/ui";
import { ago } from "./Agents";

function Item({ a, onDone }: { a: Approval; onDone: () => void }) {
  const [error, setError] = useState<string | null>(null);
  async function decide(approve: boolean) {
    const comment = approve ? undefined : window.prompt("Why not? (optional)") ?? undefined;
    try {
      await agentsApi.decide(a.id, approve, comment);
      window.dispatchEvent(new Event("pos:approvals"));
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const details = Object.entries(a.details ?? {});
  return (
    <div className="panel fade-in flex flex-col gap-3 p-4">
      <span className="flex flex-wrap items-center gap-2">
        <ActorChip a={{ kind: a.requested_by_kind, name: a.requested_by_name, is_owner: false }} />
        {a.status === "pending" ? <span className="cap text-amber-300!">needs you</span> : <span className="cap">{a.status}</span>}
        <span className="cap ml-auto">
          #{a.id} · {ago(a.created_at)}
        </span>
      </span>
      <span className="text-base">{a.action.replace(/_/g, " ")}</span>
      {details.length > 0 && (
        <div className="grid grid-cols-[110px_minmax(0,1fr)] gap-x-3 gap-y-1 rounded border border-line bg-bg p-3 text-xs">
          {details.map(([k, v]) => (
            <div key={k} className="contents">
              <span className="cap">{k.toUpperCase()}</span>
              <span className="break-words text-ink-2">{typeof v === "string" ? v : JSON.stringify(v)}</span>
            </div>
          ))}
        </div>
      )}
      {a.task_ref && (
        <Link to={`/tasks?view=agents&task=${a.task_ref}`} className="cap text-accent!">
          for task {a.task_ref} →
        </Link>
      )}
      {a.status === "pending" && (
        <span className="flex gap-2">
          <button className="btn-accent" onClick={() => decide(true)}>
            <Check size={14} /> Approve
          </button>
          <button className="btn" onClick={() => decide(false)}>
            <X size={14} /> Reject
          </button>
        </span>
      )}
      {a.comment && <span className="cap">“{a.comment}”</span>}
      {a.result && (
        <span className={`cap ${a.result.status === "sent" ? "text-accent!" : a.result.status === "failed" ? "text-red-400!" : ""}`}>
          result: {a.result.status}
          {a.result.owner_task ? ` · send it by hand: ${a.result.owner_task}` : ""}
          {a.result.error ? ` · ${a.result.error}` : ""}
        </span>
      )}
      {error && <span className="cap text-red-400!">{error}</span>}
    </div>
  );
}

export default function Approvals() {
  const [items, setItems] = useState<Approval[] | null>(null);
  const [all, setAll] = useState(false);
  const load = useCallback(() => agentsApi.approvals(all ? "all" : "pending").then(setItems), [all]);
  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="CONSTITUTION RULE 1 · NOTHING LEAVES WITHOUT YOU"
        title="Approvals"
        sub="Payments, e-mails, posts, merges, permission and limit changes: agents stop here before anything leaves PersonalOS."
      />
      <div className="flex gap-2">
        <button className={all ? "btn" : "btn-accent"} onClick={() => setAll(false)}>
          Waiting
        </button>
        <button className={all ? "btn-accent" : "btn"} onClick={() => setAll(true)}>
          History
        </button>
      </div>
      <Panel fig="TAB. 13" title={all ? "All approvals" : "Waiting for you"} right={`${items?.length ?? 0}`} bodyClassName="grid gap-3 p-3 md:grid-cols-2 xl:grid-cols-3">
        {items?.length === 0 && <p className="cap p-3">Nothing waiting. Agents will ask here before anything leaves PersonalOS.</p>}
        {items?.map((a) => (
          <Item key={a.id} a={a} onDone={load} />
        ))}
      </Panel>
    </div>
  );
}
