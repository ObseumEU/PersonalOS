import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import KnowledgeGraph from "../components/LazyGraph";
import { AssigneeChip } from "../components/tasks/bits";
import Timeline from "../components/Timeline";
import { AskBox, Legend, PageHeader, Panel, SampleBadge } from "../components/ui";
import { EVENTS, GRAPH_LABELS } from "../sample";
import { type Counts, type Task, dueLabel, tasksApi } from "../tasksApi";

const HIGHLIGHT = [0, 5, 11, 16];

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
  const refresh = () => {
    tasksApi.list("today").then(setTasks, () => setTasks([]));
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
            Your day across files, tasks and calendar. <SampleBadge />
          </span>
        }
      />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-12 lg:grid-rows-[minmax(0,1fr)_auto]">
        <Panel
          fig="FIG. 1"
          title="Knowledge graph"
          right="52 nodes · sample"
          className="h-[340px] lg:col-span-7 lg:h-auto"
          bodyClassName="measure-grid relative"
        >
          <KnowledgeGraph labels={GRAPH_LABELS} highlight={HIGHLIGHT} />
          <div className="pointer-events-none absolute bottom-3 left-4">
            <Legend items={[["#6cc4dc", "relevant today"], ["#e6e8eb", "labelled"], ["#4a515b", "other"]]} />
          </div>
          <span className="cap pointer-events-none absolute right-4 bottom-3 hidden sm:block">drag to orbit</span>
        </Panel>

        <div className="flex min-h-0 flex-col gap-4 lg:col-span-5">
          <AskBox id="ask-today" placeholder="Ask about your files, tasks and calendar" />
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
          <Panel bodyClassName="flex flex-col gap-2 px-4 py-3.5">
            <span className="flex items-baseline gap-2.5">
              <span className="cap text-accent!">NOTE</span>
              <span className="text-sm font-medium">Observation</span>
              <span className="ml-auto">
                <SampleBadge />
              </span>
            </span>
            <p className="text-sm leading-relaxed text-ink-2">
              The Acme framework agreement renews on 1 November; notice period 30 days
              <sup className="text-accent"> [1]</sup>. Decision needed by 1 October.
            </p>
            <span className="cap">[1] Acme framework agreement.pdf, p. 3, §7</span>
          </Panel>
        </div>

        <Panel fig="FIG. 2" title="Agenda" right={<SampleBadge />} className="lg:col-span-12" bodyClassName="px-5 pt-3.5 pb-1.5">
          <Timeline events={EVENTS} />
        </Panel>
      </div>
    </div>
  );
}
