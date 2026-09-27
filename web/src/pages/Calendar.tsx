import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { AssigneeChip } from "../components/tasks/bits";
import type { Task } from "../tasksApi";
import { PageHeader, Panel } from "../components/ui";

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
      <PageHeader
        kicker="CALENDAR · ICAL FEEDS · DATED TASKS"
        title="Your week."
        sub="Events from your calendars next to tasks planned or due that day."
      />
      {error && <p className="cap text-red-400!">{error}</p>}
      <Panel
        title={`${days[0].toLocaleDateString("en-GB", { day: "numeric", month: "short" })} – ${days[6].toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}`}
        right={
          <span className="flex items-center gap-3">
            <button className="hover:text-accent!" aria-label="Previous week" onClick={() => move(-1)}>
              <ChevronLeft size={14} />
            </button>
            <button className="hover:text-accent!" onClick={() => setStart(monday(new Date()))}>
              this week
            </button>
            <button className="hover:text-accent!" aria-label="Next week" onClick={() => move(1)}>
              <ChevronRight size={14} />
            </button>
          </span>
        }
      >
        <div className="grid grid-cols-1 md:grid-cols-7">
          {days.map((d) => {
            const key = iso(d);
            const events = (data?.events ?? []).filter((e) => e.start.slice(0, 10) <= key && (e.all_day ? e.end.slice(0, 10) > key : e.start.slice(0, 10) === key));
            const dated = (data?.tasks ?? []).filter((t) => t.date === key);
            return (
              <div key={key} className={`flex min-h-[180px] flex-col gap-1.5 border-b border-line p-2.5 md:border-r md:border-b-0 ${key === today ? "bg-raised" : ""}`}>
                <span className={`cap ${key === today ? "text-accent!" : ""}`}>
                  {d.toLocaleDateString("en-GB", { weekday: "short" }).toUpperCase()} {d.getDate()}
                </span>
                {events.map((e) => (
                  <div key={e.id} className="rounded border border-line bg-bg px-2 py-1.5 text-[12px] leading-snug" title={`${e.calendar}${e.location ? ` · ${e.location}` : ""}`}>
                    <span className="cap block text-accent!">{e.all_day ? "all day" : `${hm(e.start)}–${hm(e.end)}`}</span>
                    {e.title}
                  </div>
                ))}
                {dated.map((t) => (
                  <Link key={`${t.ref}-${t.kind}`} to={`/tasks?task=${t.ref}`} className="flex flex-col gap-1 rounded border border-dashed border-line px-2 py-1.5 text-[12px] leading-snug hover:border-accent">
                    <span className={`cap ${t.kind === "deadline" ? "text-amber-300!" : ""}`}>{t.kind === "deadline" ? "due" : "planned"} · {t.ref}</span>
                    {t.title}
                    <AssigneeChip type={t.assignee_type} name={t.assignee_name} />
                  </Link>
                ))}
              </div>
            );
          })}
        </div>
      </Panel>
      <Panel title="Calendars" right={data ? `${data.sources.length} connected` : "loading…"}>
        {data && !data.configured && (
          <p className="px-4 py-3 text-xs leading-relaxed text-ink-2">
            No calendar connected yet. Add your calendar's private iCal address to <span className="font-mono">POS_CALENDAR_ICS</span> in the
            server's <span className="font-mono">.env</span>: in Google Calendar, Settings → your calendar → "Secret address in iCal format"
            (Microsoft 365: Publish calendar → ICS). Several calendars: separate them with spaces, name them with{" "}
            <span className="font-mono">Name|url</span>. The address stays on the server.
          </p>
        )}
        {data?.sources.map((s) => (
          <div key={s.name} className="flex items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0">
            <span className={`h-1.5 w-1.5 rounded-full ${s.ok ? "bg-accent" : "bg-red-500"}`} />
            {s.name}
            <span className="cap ml-auto truncate">{s.ok ? "synced" : s.error}</span>
          </div>
        ))}
      </Panel>
    </div>
  );
}
