import { ArrowLeft, Plus } from "lucide-react";
import { useCallback, useEffect, useState, type DragEvent, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { AssigneeChip, StatePill } from "../components/tasks/bits";
import { PageHeader, Panel } from "../components/ui";
import { type Task, tasksApi } from "../tasksApi";

type Member = { actor_id: number; name: string; kind: string; role: "lead" | "member" };
type Project = {
  id: number;
  slug: string;
  name: string;
  goal: string | null;
  definition_of_done: string | null;
  status: "active" | "paused" | "done" | "archived";
  lead_id: number | null;
  lead_name: string | null;
  labels: string[];
  due: string | null;
  channel_id: number | null;
  members: Member[];
  counts: { queued: number; working: number; review: number; done: number };
  tasks?: Task[];
};

const post = <T,>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });
const input = "h-8 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

// Board columns and the status a card gets when dropped there.
const COLUMNS: { key: string; label: string; statuses: string[]; drop: string }[] = [
  { key: "queued", label: "Queue", statuses: ["inbox", "next", "waiting", "someday"], drop: "next" },
  { key: "working", label: "Working", statuses: ["working"], drop: "working" },
  { key: "review", label: "Review", statuses: ["review"], drop: "review" },
  { key: "done", label: "Done", statuses: ["done"], drop: "done" },
];

function ProjectList() {
  const [list, setList] = useState<Project[] | null>(null);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  useEffect(() => {
    api<Project[]>("/api/projects").then(setList, (e) => setError(e.message));
  }, []);
  const create = (e: FormEvent) => {
    e.preventDefault();
    post<Project>("/api/projects", { name, goal }).then((p) => navigate(`/projects/${p.slug}`), (e2) => setError(e2.message));
  };
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="SHARED WORK · LEAD · MEMBERS · CHANNEL"
        title="Projects"
        sub="Work people and agents share: a goal, a definition of done, a lead who reviews, members and a chat channel."
      />
      <form onSubmit={create} className="flex flex-wrap gap-2">
        <input className={`${input} w-56`} placeholder="New project" value={name} onChange={(e) => setName(e.target.value)} aria-label="Project name" />
        <input className={`${input} min-w-64 flex-1`} placeholder="Goal" value={goal} onChange={(e) => setGoal(e.target.value)} aria-label="Goal" />
        <button type="submit" className="btn-accent" disabled={!name.trim()}>
          <Plus size={14} /> Start
        </button>
      </form>
      {error && <p className="cap text-red-400!">{error}</p>}
      <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-3">
        {list?.length === 0 && <p className="cap">No projects yet.</p>}
        {list?.map((p) => (
          <Link key={p.id} to={`/projects/${p.slug}`} className="flex flex-col gap-2 rounded border border-line bg-surface p-4 hover:border-accent">
            <span className="flex items-baseline gap-2">
              <span className="text-[15px] font-medium">{p.name}</span>
              <span className="cap ml-auto">{p.status}</span>
            </span>
            {p.goal && <span className="line-clamp-2 text-[13px] text-ink-2">{p.goal}</span>}
            <span className="cap">
              lead {p.lead_name ?? "—"} · {p.members.length} members · #{p.slug}
            </span>
            <span className="cap">
              {p.counts.queued} queued · {p.counts.working} working · {p.counts.review} review · {p.counts.done} done
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}

function ProjectDetail({ slug }: { slug: string }) {
  const [p, setP] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [task, setTask] = useState("");
  const [member, setMember] = useState("");
  const load = useCallback(() => api<Project>(`/api/projects/${slug}`).then(setP, (e) => setError(e.message)), [slug]);
  useEffect(() => {
    load();
  }, [load]);
  const run = (x: Promise<unknown>) => x.then(() => load(), (e) => setError(e.message));
  const onDrop = (e: DragEvent, status: string) => {
    e.preventDefault();
    const ref = e.dataTransfer.getData("text/task");
    const t = p?.tasks?.find((x) => x.ref === ref);
    if (!t || COLUMNS.find((c) => c.drop === status)?.statuses.includes(t.status)) return;
    run(status === "done" ? tasksApi.complete(ref) : tasksApi.update(ref, { status }));
  };
  if (!p) return <p className="cap">{error ?? "Loading…"}</p>;
  return (
    <div className="flex flex-col gap-5">
      <Link to="/projects" className="cap flex items-center gap-1 hover:text-accent">
        <ArrowLeft size={12} /> projects
      </Link>
      <PageHeader kicker={`PROJECT · #${p.slug} · ${p.status.toUpperCase()}`} title={p.name} sub={p.goal ?? ""} />
      {error && <p className="cap text-red-400!">{error}</p>}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel fig="GOAL" title="Definition of done" className="lg:col-span-5">
          <p className="px-4 py-3 text-[13px] whitespace-pre-wrap">{p.definition_of_done || "Not set yet."}</p>
          <div className="flex flex-wrap gap-2 border-t border-line px-4 py-2.5">
            {(["active", "paused", "done"] as const).map((s) => (
              <button key={s} type="button" className={s === p.status ? "btn-accent" : "btn"} onClick={() => run(api(`/api/projects/${p.slug}`, { method: "PATCH", body: JSON.stringify({ status: s }) }))}>
                {s}
              </button>
            ))}
            {p.channel_id && (
              <Link to={`/chat?c=${p.channel_id}`} className="btn ml-auto">
                #{p.slug} chat →
              </Link>
            )}
          </div>
        </Panel>
        <Panel fig="TEAM" title="Members" right={`lead ${p.lead_name ?? "—"} reviews by default`} className="lg:col-span-7">
          {p.members.map((m) => (
            <div key={m.actor_id} className="flex items-center gap-2 border-b border-line px-4 py-2 text-[13px]">
              <AssigneeChip type={m.kind === "human" ? "human" : m.kind === "ai" ? "ai" : "agent"} name={m.name} />
              <span className="cap">{m.role}</span>
              {m.role !== "lead" && (
                <button type="button" className="cap ml-auto hover:text-accent" onClick={() => run(post(`/api/projects/${p.slug}/members`, { member: m.actor_id, role: "lead" }))}>
                  make lead
                </button>
              )}
            </div>
          ))}
          <form
            className="flex gap-2 px-4 py-2.5"
            onSubmit={(e) => {
              e.preventDefault();
              if (member.trim()) run(post(`/api/projects/${p.slug}/members`, { member: member.trim() }).then(() => setMember("")));
            }}
          >
            <input className={`${input} flex-1`} placeholder="Add a member by name" value={member} onChange={(e) => setMember(e.target.value)} aria-label="Add a member" />
          </form>
        </Panel>
      </div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!task.trim()) return;
          const m = task.match(/@(\S+)/);
          run(
            post("/api/tasks", { title: task.replace(/@\S+/, "").trim(), project: p.slug, status: "next", ...(m ? { assignee: m[1] } : {}) }).then(() => setTask("")),
          );
        }}
      >
        <input className={`${input} flex-1`} placeholder="Add a task… “Draft post 2 @Writer”" value={task} onChange={(e) => setTask(e.target.value)} aria-label="Add a task" />
      </form>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        {COLUMNS.map((col) => {
          const items = (p.tasks ?? []).filter((t) => col.statuses.includes(t.status));
          return (
            <div key={col.key} className="flex min-h-40 flex-col rounded border border-line bg-surface" onDragOver={(e) => e.preventDefault()} onDrop={(e) => onDrop(e, col.drop)}>
              <span className="cap border-b border-line px-3 py-2">
                {col.label} · {items.length}
              </span>
              {items.map((t) => (
                <div
                  key={t.id}
                  draggable
                  onDragStart={(e) => e.dataTransfer.setData("text/task", t.ref)}
                  className="flex cursor-grab flex-col gap-1 border-b border-line px-3 py-2 last:border-0 hover:bg-raised"
                >
                  <Link to={`/tasks?view=next&task=${t.ref}`} className="text-[13px] hover:text-accent">
                    <span className="cap mr-1.5">{t.ref}</span>
                    {t.title}
                  </Link>
                  <span className="flex items-center gap-1.5">
                    <AssigneeChip type={t.assignee_type} name={t.assignee_name} />
                    <StatePill task={t} />
                  </span>
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function Projects() {
  const { slug } = useParams();
  return slug ? <ProjectDetail slug={slug} /> : <ProjectList />;
}
