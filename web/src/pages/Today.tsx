import { useEffect, useState } from "react";
import { TaskLink } from "../taskSheet";
import { Link } from "react-router-dom";
import NeedsInbox from "../components/NeedsInbox";
import QuickAnswer from "../components/QuickAnswer";
import { AssigneeChip } from "../components/tasks/bits";
import Timeline from "../components/Timeline";
import { PageHeader, Panel } from "../components/ui";
import { type FileItem, filesApi, fmtDate } from "../filesApi";
import { LOCALE, t } from "../i18n";
import { type Counts, type Task, dueLabel, tasksApi } from "../tasksApi";
import { type Agenda, agendaApi } from "./Calendar";

function greeting(h: number) {
  if (h < 5) return t("home.hello.night");
  if (h < 12) return t("home.hello.morning");
  if (h < 18) return t("home.hello.afternoon");
  return t("home.hello.evening");
}

/** Domů: what needs you first, then today's tasks and the agenda. */
export default function Today() {
  const now = new Date();
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [agenda, setAgenda] = useState<Agenda | null>(null);
  const [recent, setRecent] = useState<FileItem[] | null>(null);
  const refresh = () => {
    tasksApi.list("today", undefined, "mine").then(setTasks, () => setTasks([]));
    const d = new Date();
    agendaApi(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`, 1).then(setAgenda, () => setAgenda(null));
    tasksApi.counts().then(setCounts, () => undefined);
    filesApi.list().then((f) => setRecent(f.slice(0, 4)), () => setRecent([]));
  };
  useEffect(refresh, []);
  const kicker = now.toLocaleDateString(LOCALE, { weekday: "long", day: "numeric", month: "long", year: "numeric" });

  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={kicker} title={greeting(now.getHours())} sub={t("home.sub")} />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <div className="flex min-w-0 flex-col gap-4 lg:col-span-7">
          <NeedsInbox />
        </div>

        <div className="flex min-w-0 flex-col gap-4 lg:col-span-5">
          <Panel
            title={t("home.today")}
            right={
              <Link to="/tasks?view=today" className="hover:text-accent">
                {counts && tasks ? t("home.today_mine", { today: tasks.length, inbox: counts.inbox }) : t("home.all_tasks")} →
              </Link>
            }
            bodyClassName="overflow-y-auto"
          >
            {tasks?.length === 0 && (
              <p className="px-4 py-5 text-sm text-ink-2">
                {t("home.nothing_today")}{" "}
                <Link to="/tasks?view=inbox" className="text-accent">
                  {t("home.clarify_inbox")} →
                </Link>
              </p>
            )}
            {tasks?.slice(0, 8).map((x) => {
              const due = dueLabel(x);
              return (
                <div key={x.id} className="grid grid-cols-[16px_minmax(0,1fr)_auto] items-center gap-3 border-b border-line px-4 py-2.5 last:border-0 sm:grid-cols-[16px_minmax(0,1fr)_auto_64px]">
                  <input
                    id={x.ref}
                    type="checkbox"
                    aria-label={t("home.complete", { title: x.title })}
                    className="h-[15px] w-[15px] accent-accent"
                    onChange={() => tasksApi.complete(x.ref).then(refresh)}
                  />
                  <TaskLink taskRef={x.ref} className="truncate text-sm hover:text-accent">
                    {x.title}
                  </TaskLink>
                  <AssigneeChip type={x.assignee_type} name={x.assignee_name} />
                  <span className={`hidden text-right text-xs sm:inline ${due.urgent ? "text-accent" : "text-ink-2"}`}>{due.text}</span>
                </div>
              );
            })}
          </Panel>
          <QuickAnswer />
        </div>

        {agenda && !agenda.configured ? (
          <p className="panel min-w-0 px-4 py-3 text-sm text-ink-2 lg:col-span-9">
            <Link to="/calendar" className="text-accent hover:underline">
              {t("home.calendar_connect")} →
            </Link>{" "}
            {t("home.calendar_connect_hint")}
          </p>
        ) : (
        <Panel
          title={t("home.agenda")}
          right={
            <Link to="/calendar" className="hover:text-accent">
              {agenda?.configured ? t("home.events_today", { n: agenda.events.length }) : t("home.no_calendar")} →
            </Link>
          }
          className="min-w-0 lg:col-span-9"
          bodyClassName="relative px-5 pt-3.5 pb-1.5"
        >
          <Timeline
            events={(agenda?.events ?? [])
              .filter((e) => !e.all_day)
              .map((e) => {
                const h = (s: string) => Number(s.slice(11, 13)) + Number(s.slice(14, 16)) / 60;
                return { start: h(e.start), end: Math.max(h(e.end), h(e.start) + 0.25), title: e.title, meta: e.location ?? e.calendar };
              })}
          />
        </Panel>
        )}

        <Panel
          title={t("home.recent_files")}
          right={
            <Link to="/knowledge?kind=file" className="hover:text-accent">
              {t("home.all_files")} →
            </Link>
          }
          className="min-w-0 lg:col-span-3"
          bodyClassName="max-h-[180px] overflow-y-auto"
        >
          {recent?.length === 0 && (
            <p className="px-4 py-4 text-sm text-ink-2">
              {t("home.no_files")}{" "}
              <Link to="/files" className="text-accent">
                {t("home.upload")} →
              </Link>
            </p>
          )}
          {recent?.map((f) => (
            <Link key={f.id} to={`/files?file=${f.id}`} className="flex items-center gap-3 border-b border-line px-4 py-2 last:border-0 hover:bg-raised">
              <span className="truncate text-sm">{f.name}</span>
              <span className="ml-auto shrink-0 text-xs text-ink-2">
                {f.topic ? `#${f.topic} · ` : ""}
                {fmtDate(f.created_at)}
              </span>
            </Link>
          ))}
        </Panel>
      </div>
    </div>
  );
}
