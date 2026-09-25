import { ArrowUpRight, CalendarDays, CheckSquare, FolderOpen, Hash, Sparkles, type LucideIcon } from "lucide-react";
import { Link } from "react-router-dom";
import PhaseBadge from "../components/PhaseBadge";

function greeting(hour: number) {
  if (hour < 5) return "Good night";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

function Widget({
  to,
  icon: Icon,
  title,
  phase,
  empty,
  hint,
  className = "",
  delay = 0,
}: {
  to: string;
  icon: LucideIcon;
  title: string;
  phase: number;
  empty: string;
  hint: string;
  className?: string;
  delay?: number;
}) {
  return (
    <Link
      to={to}
      className={`card rise group flex flex-col p-5 transition hover:-translate-y-0.5 hover:border-brand/50 ${className}`}
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="flex items-center gap-3">
        <div className="grid h-9 w-9 place-items-center rounded-xl bg-surface-2 text-brand">
          <Icon size={18} />
        </div>
        <h2 className="font-semibold tracking-tight">{title}</h2>
        <ArrowUpRight
          size={16}
          className="ml-auto text-ink-3 opacity-0 transition group-hover:opacity-100"
        />
      </div>
      <div className="flex flex-1 flex-col items-center justify-center py-8 text-center">
        <p className="text-sm font-medium text-ink-2">{empty}</p>
        <p className="mt-1 max-w-xs text-xs text-ink-3">{hint}</p>
      </div>
      <PhaseBadge phase={phase} />
    </Link>
  );
}

export default function Today() {
  const now = new Date();
  const date = now.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });

  return (
    <div className="space-y-8">
      <header className="rise">
        <p className="text-sm font-medium text-ink-3">{date}</p>
        <h1 className="mt-1 text-3xl font-semibold tracking-tight sm:text-4xl">
          {greeting(now.getHours())}. <span className="text-gradient">What's on today?</span>
        </h1>
      </header>

      <div className="card rise relative overflow-hidden p-2" style={{ animationDelay: "60ms" }}>
        <div className="brand-gradient pointer-events-none absolute -top-24 -right-24 h-48 w-48 rounded-full opacity-20 blur-3xl" />
        <form
          className="relative flex items-center gap-3 rounded-2xl bg-surface px-4 py-3"
          onSubmit={(e) => e.preventDefault()}
        >
          <Sparkles size={20} className="shrink-0 text-brand" />
          <input
            disabled
            placeholder="Ask about your files, tasks and calendar…"
            className="min-w-0 flex-1 bg-transparent text-[0.95rem] outline-none placeholder:text-ink-3 disabled:cursor-not-allowed"
          />
          <PhaseBadge phase={3} compact />
        </form>
        <div className="relative flex flex-wrap gap-2 px-3 pt-3 pb-2">
          {["What's open for client X this week?", "Summarize yesterday's contract", "Plan my Friday"].map((q) => (
            <span
              key={q}
              className="rounded-full border border-line bg-surface-2 px-3 py-1 text-xs text-ink-2"
            >
              {q}
            </span>
          ))}
        </div>
      </div>

      <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
        <Widget
          to="/calendar"
          icon={CalendarDays}
          title="Agenda"
          phase={4}
          empty="No calendar connected yet"
          hint="Your Google or Microsoft 365 events will show up here."
          className="xl:row-span-2"
          delay={120}
        />
        <Widget
          to="/tasks"
          icon={CheckSquare}
          title="Due soon"
          phase={2}
          empty="Nothing due"
          hint="Tasks with a due date in the next few days land here."
          delay={170}
        />
        <Widget
          to="/files"
          icon={FolderOpen}
          title="Recent files"
          phase={2}
          empty="No files yet"
          hint="Drop documents in Files and the latest appear here."
          delay={220}
        />
        <Widget
          to="/topics"
          icon={Hash}
          title="Topics"
          phase={2}
          empty="No topics yet"
          hint="Clients, projects, health, house: one home for each."
          className="md:col-span-2"
          delay={270}
        />
      </div>
    </div>
  );
}
