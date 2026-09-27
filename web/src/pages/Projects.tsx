import { ArrowLeft, Plus } from "lucide-react";
import { useCallback, useEffect, useState, type DragEvent, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { AssigneeChip, StatePill } from "../components/tasks/bits";
import { PageHeader, Panel } from "../components/ui";
import { plural, t } from "../i18n";
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
const input = "h-8 min-w-0 rounded border border-line bg-bg px-2 text-[13px] outline-none focus:border-accent";

const statusWord = (s: string) => t(`work.projects.status.${s}`);

// Board columns and the status a card gets when dropped there.
const COLUMNS: { key: string; statuses: string[]; drop: string }[] = [
  { key: "queued", statuses: ["inbox", "next", "waiting", "someday"], drop: "next" },
  { key: "working", statuses: ["working"], drop: "working" },
  { key: "review", statuses: ["review"], drop: "review" },
  { key: "done", statuses: ["done"], drop: "done" },
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
      <PageHeader kicker={t("work.projects.kicker")} title={t("nav.projects")} sub={t("work.projects.sub")} />
      <form onSubmit={create} className="flex flex-wrap gap-2">
        <input
          className={`${input} w-full sm:w-56`}
          placeholder={t("work.projects.new")}
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label={t("work.projects.name_aria")}
        />
        <input
          className={`${input} w-full flex-1 sm:w-auto sm:min-w-64`}
          placeholder={t("work.projects.goal")}
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          aria-label={t("work.projects.goal")}
        />
        <button type="submit" className="btn-accent" disabled={!name.trim()}>
          <Plus size={14} /> {t("work.projects.start")}
        </button>
      </form>
      {error && <p className="text-xs break-words text-red-400">{error}</p>}
      <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-3">
        {list?.length === 0 && <p className="text-sm text-ink-2">{t("work.projects.empty")}</p>}
        {list?.map((p) => (
          <Link key={p.id} to={`/projects/${p.slug}`} className="flex min-w-0 flex-col gap-2 rounded border border-line bg-surface p-4 hover:border-accent">
            <span className="flex min-w-0 items-baseline gap-2">
              <span className="min-w-0 truncate text-[15px] font-medium">{p.name}</span>
              <span className="ml-auto shrink-0 text-xs text-ink-2">{statusWord(p.status)}</span>
            </span>
            {p.goal && <span className="line-clamp-2 text-[13px] break-words text-ink-2">{p.goal}</span>}
            <span className="text-xs break-words text-ink-2">
              {t("work.projects.card_meta", {
                lead: p.lead_name ?? "—",
                n: p.members.length,
                word: plural(p.members.length, t("work.word.member1"), t("work.word.member2"), t("work.word.member5")),
                slug: p.slug,
              })}
            </span>
            <span className="text-xs break-words text-ink-2">{t("work.projects.card_counts", p.counts)}</span>
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
    const found = p?.tasks?.find((x) => x.ref === ref);
    if (!found || COLUMNS.find((c) => c.drop === status)?.statuses.includes(found.status)) return;
    run(status === "done" ? tasksApi.complete(ref) : tasksApi.update(ref, { status }));
  };
  if (!p) return <p className={error ? "text-xs break-words text-red-400" : "text-sm text-ink-2"}>{error ?? t("act.loading")}</p>;
  return (
    <div className="flex flex-col gap-5">
      <Link to="/projects" className="flex items-center gap-1 self-start text-xs text-ink-2 hover:text-accent">
        <ArrowLeft size={12} /> {t("nav.projects")}
      </Link>
      <PageHeader kicker={t("work.projects.kicker_detail", { slug: p.slug, status: statusWord(p.status) })} title={p.name} sub={p.goal ?? ""} />
      {error && <p className="text-xs break-words text-red-400">{error}</p>}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel title={t("work.projects.dod")} className="min-w-0 lg:col-span-5">
          <p className="px-4 py-3 text-[13px] break-words whitespace-pre-wrap">{p.definition_of_done || t("work.projects.dod_unset")}</p>
          <div className="flex flex-wrap gap-2 border-t border-line px-4 py-2.5">
            {(["active", "paused", "done"] as const).map((s) => (
              <button
                key={s}
                type="button"
                className={s === p.status ? "btn-accent" : "btn"}
                onClick={() => run(api(`/api/projects/${p.slug}`, { method: "PATCH", body: JSON.stringify({ status: s }) }))}
              >
                {t(`work.projects.set.${s}`)}
              </button>
            ))}
            {p.channel_id && (
              <Link to={`/chat?c=${p.channel_id}`} className="btn ml-auto">
                {t("work.projects.chat", { slug: p.slug })}
              </Link>
            )}
          </div>
        </Panel>
        <Panel
          title={t("work.projects.members")}
          right={t("work.projects.members_right", { lead: p.lead_name ?? "—" })}
          className="min-w-0 lg:col-span-7"
        >
          {p.members.map((m) => (
            <div key={m.actor_id} className="flex min-w-0 items-center gap-2 border-b border-line px-4 py-2 text-[13px]">
              <span className="min-w-0">
                <AssigneeChip type={m.kind === "human" ? "human" : m.kind === "ai" ? "ai" : "agent"} name={m.name} />
              </span>
              <span className="shrink-0 text-xs text-ink-2">{t(`work.projects.role.${m.role}`)}</span>
              {m.role !== "lead" && (
                <button
                  type="button"
                  className="ml-auto shrink-0 text-xs text-ink-2 hover:text-accent"
                  onClick={() => run(post(`/api/projects/${p.slug}/members`, { member: m.actor_id, role: "lead" }))}
                >
                  {t("work.projects.make_lead")}
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
            <input
              className={`${input} flex-1`}
              placeholder={t("work.projects.add_member")}
              value={member}
              onChange={(e) => setMember(e.target.value)}
              aria-label={t("work.projects.add_member_aria")}
            />
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
        <input
          className={`${input} flex-1`}
          placeholder={t("work.projects.add_task")}
          value={task}
          onChange={(e) => setTask(e.target.value)}
          aria-label={t("work.projects.add_task_aria")}
        />
      </form>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        {COLUMNS.map((col) => {
          const items = (p.tasks ?? []).filter((x) => col.statuses.includes(x.status));
          return (
            <div
              key={col.key}
              className="flex min-h-40 min-w-0 flex-col rounded border border-line bg-surface"
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => onDrop(e, col.drop)}
            >
              <span className="border-b border-line px-3 py-2 text-xs text-ink-2">
                {t(`work.projects.col.${col.key}`)} · {items.length}
              </span>
              {items.map((x) => (
                <div
                  key={x.id}
                  draggable
                  onDragStart={(e) => e.dataTransfer.setData("text/task", x.ref)}
                  className="flex min-w-0 cursor-grab flex-col gap-1 border-b border-line px-3 py-2 last:border-0 hover:bg-raised"
                >
                  <Link to={`/tasks?view=next&task=${x.ref}`} className="text-[13px] break-words hover:text-accent">
                    <span className="mr-1.5 font-mono text-xs text-ink-2">{x.ref}</span>
                    {x.title}
                  </Link>
                  <span className="flex min-w-0 flex-wrap items-center gap-1.5">
                    <AssigneeChip type={x.assignee_type} name={x.assignee_name} />
                    <StatePill task={x} />
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
