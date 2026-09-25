import { Archive, Pause, Play, Plus, Zap } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ago } from "../pages/Agents";
import { Panel } from "./ui";

export type Schedule = {
  id: number;
  name: string;
  schedule: string;
  template: { title?: string; notes?: string; topic?: string };
  visibility: "personal" | "team";
  status: "active" | "paused";
  next_run_at: string | null;
  last_run_at: string | null;
  last_result: Record<string, unknown> | null;
  last_task_ref: string | null;
  runs: number;
  created_by: number;
  created_by_name: string;
  assignee_id: number;
  assignee_name: string;
};

export function until(iso: string | null) {
  if (!iso) return "—";
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  if (s < 60) return "in < 1 min";
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  if (s < 86400) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} d`;
}

export function resultText(r: Record<string, unknown> | null) {
  if (!r) return "—";
  if (r.error) return `error: ${r.error}`;
  if (r.skipped) return String(r.skipped);
  if (r.deferred) return `deferred: ${r.deferred}`;
  if (r.task) return `created ${r.task}`;
  return (
    Object.entries(r)
      .filter(([, v]) => v !== null && !(Array.isArray(v) && v.length === 0) && typeof v !== "object")
      .map(([k, v]) => `${k}: ${v}`)
      .join(" · ") || "ok"
  );
}

const COLS = "md:grid-cols-[minmax(0,1.4fr)_110px_minmax(0,1fr)_80px_minmax(0,1.2fr)_96px]";

function Rows({ items, onChange, onError }: { items: Schedule[]; onChange: () => void; onError: (m: string | null) => void }) {
  const act = (p: Promise<unknown>) => p.then(() => { onError(null); onChange(); }, (e) => onError(e.message));
  if (items.length === 0) return <p className="cap px-4 py-3">None yet.</p>;
  return (
    <>
      <div className={`hidden ${COLS} gap-3 border-b border-line px-4 py-2 md:grid`}>
        {["SCHEDULE", "WHEN", "OWNER → ASSIGNEE", "NEXT", "LAST RESULT", ""].map((h) => (
          <span key={h} className="cap">{h}</span>
        ))}
      </div>
      {items.map((s) => (
        <div key={s.id} className={`grid grid-cols-1 gap-1.5 border-b border-line px-4 py-2.5 text-[13px] ${COLS} md:items-center md:gap-3 ${s.status === "paused" ? "opacity-50" : ""}`}>
          <span className="flex min-w-0 flex-col">
            <span className="truncate">{s.name}</span>
            {s.template.notes && <span className="cap truncate">{s.template.notes}</span>}
          </span>
          <span className="font-mono text-xs">{s.schedule}</span>
          <span className="cap truncate">
            <Link to={`/agents/${s.created_by}`} className="hover:text-accent!">{s.created_by_name}</Link>
            {s.assignee_id !== s.created_by && (
              <> → <Link to={`/agents/${s.assignee_id}`} className="hover:text-accent!">{s.assignee_name}</Link></>
            )}
          </span>
          <span className="cap">{s.status === "active" ? until(s.next_run_at) : "paused"}</span>
          <span className={`cap truncate ${s.last_result?.error ? "text-red-400!" : ""}`} title={s.last_run_at ? `ran ${ago(s.last_run_at)} · ${s.runs} tasks so far` : ""}>
            {s.last_task_ref ? (
              <Link to={`/tasks?task=${s.last_task_ref}`} className="hover:text-accent!">{resultText(s.last_result)}</Link>
            ) : (
              resultText(s.last_result)
            )}
          </span>
          <span className="flex justify-end gap-2.5">
            <button className="cap hover:text-accent!" title="Fire now" aria-label={`Fire ${s.name} now`} onClick={() => act(api(`/api/schedules/${s.id}/run`, { method: "POST" }))}>
              <Zap size={13} />
            </button>
            <button
              className="cap hover:text-accent!"
              title={s.status === "active" ? "Pause" : "Resume"}
              aria-label={`${s.status === "active" ? "Pause" : "Resume"} ${s.name}`}
              onClick={() => act(api(`/api/schedules/${s.id}`, { method: "PATCH", body: JSON.stringify({ status: s.status === "active" ? "paused" : "active" }) }))}
            >
              {s.status === "active" ? <Pause size={13} /> : <Play size={13} />}
            </button>
            <button className="cap hover:text-accent!" title="Archive" aria-label={`Archive ${s.name}`} onClick={() => act(api(`/api/schedules/${s.id}/archive`, { method: "POST" }))}>
              <Archive size={13} />
            </button>
          </span>
        </div>
      ))}
    </>
  );
}

export function NewSchedule({ assignee, onCreated, onError }: { assignee?: { id: number; name: string }; onCreated: () => void; onError: (m: string | null) => void }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ name: "", schedule: "daily 07:00", notes: "" });
  if (!open)
    return (
      <button className="btn h-7!" onClick={() => setOpen(true)}>
        <Plus size={12} /> New schedule
      </button>
    );
  const submit = () =>
    api("/api/schedules", {
      method: "POST",
      body: JSON.stringify({
        ...f,
        visibility: assignee ? "team" : "personal",
        assignee: assignee ? { type: "agent", id: assignee.id } : null,
      }),
    }).then(() => { setOpen(false); setF({ name: "", schedule: "daily 07:00", notes: "" }); onError(null); onCreated(); }, (e) => onError(e.message));
  return (
    <div className="flex flex-wrap items-center gap-2">
      <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder={assignee ? `What should ${assignee.name} do?` : "What should happen?"} aria-label="Schedule name" className="h-7 min-w-44 flex-1 rounded border border-line bg-bg px-2 text-xs outline-none focus:border-accent" />
      <input value={f.schedule} onChange={(e) => setF({ ...f, schedule: e.target.value })} aria-label="When" title="every 30m · every 2h · daily 07:00 · weekdays 07:00 · weekly fri 15:00" className="h-7 w-32 rounded border border-line bg-bg px-2 font-mono text-xs outline-none focus:border-accent" />
      <input value={f.notes} onChange={(e) => setF({ ...f, notes: e.target.value })} placeholder="Notes for the task" aria-label="Notes" className="h-7 min-w-44 flex-1 rounded border border-line bg-bg px-2 text-xs outline-none focus:border-accent" />
      <button className="btn h-7!" disabled={!f.name} onClick={submit}>Create</button>
      <button className="cap hover:text-accent!" onClick={() => setOpen(false)}>cancel</button>
    </div>
  );
}

/** Schedules of one member (split into its personal and team ones), or everyone's. */
export function SchedulesPanel({ actor, fig = "AUTO" }: { actor?: { id: number; name: string }; fig?: string }) {
  const [items, setItems] = useState<Schedule[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<Schedule[]>(actor ? `/api/schedules?actor_id=${actor.id}` : "/api/schedules").then(setItems);
  }, [actor]);
  useEffect(() => {
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, [load]);
  const personal = items.filter((s) => s.visibility === "personal");
  const team = items.filter((s) => s.visibility === "team");
  const header = (
    <div className="flex flex-col gap-2 border-b border-line px-4 py-2.5">
      <NewSchedule assignee={actor} onCreated={load} onError={setError} />
      {error && <p className="cap text-red-400!">{error}</p>}
    </div>
  );
  if (!actor)
    return (
      <Panel fig={fig} title="Schedules of people and agents" right="each firing creates a task · agents: max 5, every ≥ 15 min">
        {header}
        <Rows items={items} onChange={load} onError={setError} />
      </Panel>
    );
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Panel fig={fig} title="Automations · personal" right={`${actor.name}'s own routines`}>
        <Rows items={personal} onChange={load} onError={setError} />
      </Panel>
      <Panel fig={fig} title="Automations · team" right="shared work it owns or is assigned">
        {header}
        <Rows items={team} onChange={load} onError={setError} />
      </Panel>
    </div>
  );
}
