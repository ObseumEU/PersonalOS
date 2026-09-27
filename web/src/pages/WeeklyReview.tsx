import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { AssigneeChip, StatePill } from "../components/tasks/bits";
import { PageHeader, Panel } from "../components/ui";
import type { Task } from "../tasksApi";

type Review = {
  inbox: Task[];
  waiting: Task[];
  someday: Task[];
  to_review: Task[];
  projects_without_next: { id: number; slug: string; name: string; lead_name: string | null }[];
};

function TaskList({ items, empty, view }: { items: Task[]; empty: string; view: string }) {
  if (!items.length) return <p className="cap px-4 py-3">{empty}</p>;
  return (
    <>
      {items.slice(0, 12).map((t) => (
        <Link key={t.id} to={`/tasks?view=${view}&task=${t.ref}`} className="flex items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0 hover:bg-raised">
          <span className="cap">{t.ref}</span>
          <span className="min-w-0 flex-1 truncate">{t.title}</span>
          <StatePill task={t} />
          <AssigneeChip type={t.assignee_type} name={t.assignee_name} />
        </Link>
      ))}
    </>
  );
}

/** The GTD weekly review for the signed-in member (agents do it as a prompt). */
export default function WeeklyReview() {
  const [r, setR] = useState<Review | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<Review>("/api/weekly-review").then(setR, (e) => setError(e.message));
  }, []);
  // What can be "clear": an empty inbox, a next step in every project, nothing left to review.
  const clear = r ? [r.inbox.length === 0, r.projects_without_next.length === 0, r.to_review.length === 0] : [];
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="GTD · WEEKLY REVIEW"
        title="Weekly review"
        sub={r ? `${clear.filter(Boolean).length} of 3 checks clear · get clear, get current, get creative` : "Loading…"}
      />
      {error && <p className="cap text-red-400!">{error}</p>}
      {r && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Panel title="Inbox to zero" right={<Link to="/tasks/inbox" className="hover:text-accent">clarify one by one →</Link>}>
            <TaskList items={r.inbox} empty="Inbox is empty." view="inbox" />
          </Panel>
          <Panel title="Waiting for" right="chase what is late">
            <TaskList items={r.waiting} empty="Nobody owes you anything." view="waiting" />
          </Panel>
          <Panel title="Projects without a next step" right="give each one a next action">
            {r.projects_without_next.length === 0 && <p className="cap px-4 py-3">Every active project has a next step.</p>}
            {r.projects_without_next.map((p) => (
              <Link key={p.id} to={`/projects/${p.slug}`} className="flex items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0 hover:bg-raised">
                <span className="min-w-0 flex-1 truncate">{p.name}</span>
                <span className="cap">lead {p.lead_name ?? "—"}</span>
              </Link>
            ))}
          </Panel>
          <Panel title="Waiting for your review" right="accept or return">
            <TaskList items={r.to_review} empty="Nothing to review." view="review" />
          </Panel>
          <Panel title="Someday / maybe" right="anything to start now?" className="lg:col-span-2">
            <TaskList items={r.someday} empty="Nothing parked." view="someday" />
          </Panel>
        </div>
      )}
    </div>
  );
}
