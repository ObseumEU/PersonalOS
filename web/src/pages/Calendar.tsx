import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { AssigneeChip } from "../components/tasks/bits";
import type { Task } from "../tasksApi";
import { PageHeader, Panel } from "../components/ui";
import { LOCALE, t } from "../i18n";

export type CalEvent = { id: string; title: string; start: string; end: string; all_day: boolean; location: string | null; calendar: string };
export type DatedTask = { ref: string; title: string; date: string; kind: "do" | "deadline"; status: string; assignee_type: Task["assignee_type"]; assignee_name: string | null };
export type Agenda = {
  start: string;
  days: number;
  configured: boolean;
  sources: { name: string; ok: boolean; error?: string }[];
  events: CalEvent[];
  tasks: DatedTask[];
  /** The first day: working hours minus timed events (POS_WORK_HOURS). */
  capacity?: { work_min: number; meetings_min: number; free_min: number };
};

export const agendaApi = (start: string, days = 7) => api<Agenda>(`/api/calendar?start=${start}&days=${days}`);

const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const hm = (s: string) => s.slice(11, 16);

function monday(d: Date) {
  const x = new Date(d);
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}

export default function Calendar() {
  const [start, setStart] = useState(() => monday(new Date()));
  const [data, setData] = useState<Agenda | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setData(null);
    agendaApi(iso(start)).then(setData, (e) => setError(e.message));
  }, [start]);
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + i)), [start]);
  const today = iso(new Date());
  const move = (weeks: number) => setStart(new Date(start.getFullYear(), start.getMonth(), start.getDate() + weeks * 7));

  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={t("work.cal.kicker")} title={t("work.cal.title")} sub={t("work.cal.sub")} />
      {error && <p className="text-xs break-words text-red-400">{error}</p>}
      <Panel
        title={`${days[0].toLocaleDateString(LOCALE, { day: "numeric", month: "short" })} – ${days[6].toLocaleDateString(LOCALE, { day: "numeric", month: "short", year: "numeric" })}`}
        right={
          <span className="flex items-center gap-3">
            <button className="hover:text-accent!" aria-label={t("work.cal.prev")} title={t("work.cal.prev")} onClick={() => move(-1)}>
              <ChevronLeft size={14} />
            </button>
            <button className="hover:text-accent!" onClick={() => setStart(monday(new Date()))}>
              {t("work.cal.this_week")}
            </button>
            <button className="hover:text-accent!" aria-label={t("work.cal.next")} title={t("work.cal.next")} onClick={() => move(1)}>
              <ChevronRight size={14} />
            </button>
          </span>
        }
      >
        <div className="grid grid-cols-1 md:grid-cols-7">
          {days.map((d) => {
            const key = iso(d);
            const events = (data?.events ?? []).filter((e) => e.start.slice(0, 10) <= key && (e.all_day ? e.end.slice(0, 10) > key : e.start.slice(0, 10) === key));
            const dated = (data?.tasks ?? []).filter((x) => x.date === key);
            return (
              <div
                key={key}
                className={`flex min-w-0 flex-col gap-1.5 border-b border-line p-2.5 md:min-h-[180px] md:border-r md:border-b-0 ${key === today ? "bg-raised" : ""}`}
              >
                <span className={`text-xs font-medium ${key === today ? "text-accent" : "text-ink-2"}`}>
                  {d.toLocaleDateString(LOCALE, { weekday: "short" })} {d.getDate()}. {d.getMonth() + 1}.
                </span>
                {events.map((e) => (
                  <div
                    key={e.id}
                    className="rounded border border-line bg-bg px-2 py-1.5 text-xs leading-snug break-words"
                    title={`${e.calendar}${e.location ? ` · ${e.location}` : ""}`}
                  >
                    <span className="block text-accent tabular-nums">{e.all_day ? t("work.cal.all_day") : `${hm(e.start)}–${hm(e.end)}`}</span>
                    {e.title}
                  </div>
                ))}
                {dated.map((x) => (
                  <Link
                    key={`${x.ref}-${x.kind}`}
                    to={`/tasks?task=${x.ref}`}
                    className="flex min-w-0 flex-col gap-1 rounded border border-dashed border-line px-2 py-1.5 text-xs leading-snug break-words hover:border-accent"
                  >
                    <span className={x.kind === "deadline" ? "text-amber-300" : "text-ink-2"}>
                      {x.kind === "deadline" ? t("work.cal.due") : t("work.cal.planned")} · <span className="font-mono">{x.ref}</span>
                    </span>
                    {x.title}
                    <span className="min-w-0">
                      <AssigneeChip type={x.assignee_type} name={x.assignee_name} />
                    </span>
                  </Link>
                ))}
              </div>
            );
          })}
        </div>
      </Panel>
      <Panel title={t("work.cal.calendars")} right={data ? t("work.cal.connected", { n: data.sources.length }) : t("act.loading")}>
        {data && !data.configured && (
          <p className="px-4 py-3 text-xs leading-relaxed break-words text-ink-2">
            {t("work.cal.setup1")} <span className="font-mono">POS_CALENDAR_ICS</span> {t("work.cal.setup2")}{" "}
            <span className="font-mono">.env</span> {t("work.cal.setup3")} <span className="font-mono">Name|url</span>
            {t("work.cal.setup4")}
          </p>
        )}
        {data?.sources.map((s) => (
          <div key={s.name} className="flex min-w-0 items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0">
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${s.ok ? "bg-accent" : "bg-red-500"}`} />
            <span className="min-w-0 truncate">{s.name}</span>
            <span className="ml-auto min-w-0 truncate text-xs text-ink-2">{s.ok ? t("work.cal.synced") : s.error}</span>
          </div>
        ))}
      </Panel>
    </div>
  );
}