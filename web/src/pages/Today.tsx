import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import KnowledgePanel from "../components/KnowledgePanel";
import { AssigneeChip } from "../components/tasks/bits";
import Timeline from "../components/Timeline";
import { AskBox, PageHeader, Panel } from "../components/ui";
import { type Counts, type Task, dueLabel, tasksApi } from "../tasksApi";

function greeting(h: number) {
  if (h < 5) return "Good night";
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

export default function Today() {
  const now = new Date();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [review, setReview] = useState<Task[] | null>(null);
  const refresh = () => {
    tasksApi.list("today").then(setTasks, () => setTasks([]));
    tasksApi.list("review").then(setReview, () => setReview([]));
    tasksApi.counts().then(setCounts, () => undefined);
  };
  useEffect(refresh, []);
  const kicker = now
    .toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" })
    .toUpperCase();

  return (
    <div className="flex flex-col gap-5 lg:h-[calc(100vh-3rem)]">
      <PageHeader
        kicker={kicker}
        title={`${greeting(now.getHours())}.`}
        sub={
          <span className="flex flex-wrap items-center gap-2">
            Your day across tasks, agents and the knowledge base.
          </span>
        }
      />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-12 lg:grid-rows-[minmax(0,1fr)_auto]">
        <KnowledgePanel fig="FIG. 1" className="h-[340px] lg:col-span-7 lg:h-auto" />

        <div className="flex min-h-0 flex-col gap-4 lg:col-span-5">
          <AskBox id="ask-today" placeholder="Ask our knowledge base (answers cite their sources)" />
          <Panel
            fig="TAB. 1"
            title="Today"
            right={
              <Link to="/tasks?view=today" className="hover:text-accent">
                {counts ? `${counts.today} planned · ${counts.inbox} in inbox` : "all tasks"} →
              </Link>
            }
            bodyClassName="overflow-y-auto"
          >
            {tasks?.length === 0 && (
              <p className="cap px-4 py-5">
                Nothing planned for today.{" "}
                <Link to="/tasks?view=inbox" className="text-accent!">
                  Clarify the inbox →
                </Link>
              </p>
            )}
            {tasks?.slice(0, 6).map((t) => {
              const due = dueLabel(t);
              return (
                <div key={t.id} className="grid grid-cols-[16px_minmax(0,1fr)_auto_64px] items-center gap-3 border-b border-line px-4 py-2.5 last:border-0">
                  <input
                    id={t.ref}
                    type="checkbox"
                    className="h-[15px] w-[15px] accent-accent"
                    onChange={() => tasksApi.complete(t.ref).then(refresh)}
                  />
                  <Link to={`/tasks?view=today&task=${t.ref}`} className="truncate text-sm hover:text-accent">
                    {t.title}
                  </Link>
                  <AssigneeChip type={t.assignee_type} name={t.assignee_name} />
                  <span className={`cap text-right ${due.urgent ? "text-accent!" : ""}`}>{due.text}</span>
                </div>
              );
            })}
          </Panel>
          <Panel
            fig="REVIEW"
            title="Handed in by agents"
            right={
              <Link to="/tasks?view=review" className="hover:text-accent">
                {review ? `${review.length} to review` : "review"} →
              </Link>
            }
            bodyClassName="max-h-[180px] overflow-y-auto"
          >
            {review?.length === 0 && <p className="cap px-4 py-4">Nothing waiting for your review.</p>}
            {review?.slice(0, 4).map((t) => (
              <Link key={t.id} to={`/tasks?view=review&task=${t.ref}`} className="flex flex-col gap-0.5 border-b border-line px-4 py-2.5 last:border-0 hover:bg-raised">
                <span className="flex items-center gap-2">
                  <span className="truncate text-sm">{t.title}</span>
                  <span className="ml-auto shrink-0">
                    <AssigneeChip type={t.assignee_type} name={t.assignee_name} />
                  </span>
                </span>
                {t.progress_note && <span className="cap truncate">{t.progress_note}</span>}
              </Link>
            ))}
          </Panel>
        </div>

        <Panel fig="FIG. 2" title="Agenda" right="no calendar connected yet" mock="calendar sync not built; no events yet" className="lg:col-span-12" bodyClassName="relative px-5 pt-3.5 pb-1.5">
          <Timeline events={[]} />
          <p className="cap pointer-events-none absolute inset-0 grid place-items-center">
            Your calendar appears here once the Google Calendar connector is set up (Connectors).
          </p>
        </Panel>
      </div>
    </div>
  );
}
