import { useEffect, useState } from "react";
import { TaskLink } from "../taskSheet";
import { Link } from "react-router-dom";
import { api } from "../api";
import { AssigneeChip, StatePill } from "../components/tasks/bits";
import { PageHeader, Panel } from "../components/ui";
import { t } from "../i18n";
import type { ReviewQueue, Task } from "../tasksApi";

type Review = {
  inbox: Task[];
  waiting: Task[];
  someday: Task[];
  to_review: Task[];
  review_queue?: ReviewQueue;
  projects_without_next: { id: number; slug: string; name: string; lead_name: string | null }[];
};

function TaskList({ items, empty, view }: { items: Task[]; empty: string; view: string }) {
  if (!items.length) return <p className="px-4 py-3 text-sm text-ink-2">{empty}</p>;
  return (
    <>
      {items.slice(0, 12).map((task) => (
        <TaskLink
          key={task.id}
          taskRef={task.ref}
          className="flex min-w-0 items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0 hover:bg-raised"
        >
          <span className="hidden shrink-0 font-mono text-xs text-ink-2 sm:inline">{task.ref}</span>
          <span className="min-w-0 flex-1 truncate">{task.title}</span>
          <StatePill task={task} />
          <span className="max-w-[40%] min-w-0 shrink-0">
            <AssigneeChip type={task.assignee_type} name={task.assignee_name} />
          </span>
        </TaskLink>
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
        kicker={t("work.weekly.kicker")}
        title={t("nav.weekly_review")}
        sub={r ? t("work.weekly.sub", { n: clear.filter(Boolean).length }) : t("act.loading")}
      />
      {error && <p className="text-xs break-words text-red-400">{error}</p>}
      {r && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Panel
            title={t("work.weekly.inbox")}
            right={
              <Link to="/tasks/inbox" className="hover:text-accent">
                {t("work.weekly.inbox_right")}
              </Link>
            }
          >
            <TaskList items={r.inbox} empty={t("work.weekly.inbox_empty")} view="inbox" />
          </Panel>
          <Panel title={t("work.weekly.waiting")} right={t("work.weekly.waiting_right")}>
            <TaskList items={r.waiting} empty={t("work.weekly.waiting_empty")} view="waiting" />
          </Panel>
          <Panel title={t("work.weekly.projects")} right={t("work.weekly.projects_right")}>
            {r.projects_without_next.length === 0 && <p className="px-4 py-3 text-sm text-ink-2">{t("work.weekly.projects_empty")}</p>}
            {r.projects_without_next.map((p) => (
              <Link
                key={p.id}
                to={`/projects/${p.slug}`}
                className="flex min-w-0 items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0 hover:bg-raised"
              >
                <span className="min-w-0 flex-1 truncate">{p.name}</span>
                <span className="shrink-0 text-xs text-ink-2">{t("work.weekly.lead", { name: p.lead_name ?? "—" })}</span>
              </Link>
            ))}
          </Panel>
          <Panel title={t("work.weekly.review")} right={t("work.weekly.review_right")}>
            {r.review_queue && <p className="border-b border-line px-4 py-2 text-[13px] text-ink-2">{r.review_queue.text}</p>}
            <TaskList items={r.to_review} empty={t("work.weekly.review_empty")} view="review" />
          </Panel>
          <Panel title={t("work.weekly.someday")} right={t("work.weekly.someday_right")} className="lg:col-span-2">
            <TaskList items={r.someday} empty={t("work.weekly.someday_empty")} view="someday" />
          </Panel>
        </div>
      )}
    </div>
  );
}
