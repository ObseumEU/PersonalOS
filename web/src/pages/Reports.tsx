import { ArrowDownRight, ArrowUpRight, ChevronLeft, ChevronRight, Minus, Printer, RefreshCw } from "lucide-react";
import { TaskLink } from "../taskSheet";
import { Fragment, useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend as ChartLegend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";
import Markdown from "../components/Markdown";
import { PageHeader, Panel } from "../components/ui";
import { LOCALE, label, t } from "../i18n";
import { type Kpi, type Packet, type PacketGoal, type Report, type ReportListItem, type ReportStatus, reportsApi } from "../reportsApi";

const STATUS_TONE: Record<ReportStatus, string> = {
  draft: "text-ink-2",
  meeting: "text-amber-300",
  published: "text-accent",
  closed: "text-accent",
  no_reply: "text-ink-2",
};
const statusWord = (s: ReportStatus) => t(`reports.status.${s}`);

/** Which way is good for a KPI: up (more done), down (less waiting), or neutral. */
const KPIS: { key: string; good: "up" | "down" | "none"; fmt?: (v: number) => string }[] = [
  { key: "tasks_done", good: "up" },
  { key: "tasks_new", good: "none" },
  { key: "waiting", good: "down" },
  { key: "overdue", good: "down" },
  { key: "agent_success_rate", good: "up", fmt: (v) => `${Math.round(v * 100)} %` },
  { key: "agent_cost_usd", good: "down", fmt: (v) => `$${v.toFixed(2)}` },
  { key: "commits", good: "up" },
  { key: "deploys", good: "up" },
  { key: "communication", good: "none" },
  { key: "business_outcomes", good: "up" },
  { key: "usd_per_business_outcome", good: "down", fmt: (v) => `$${v.toFixed(2)}` },
  { key: "owner_minutes", good: "down" },
  { key: "customer_threads_open", good: "down" },
  { key: "drafts_in_approvals", good: "up" },
  { key: "invoices_sent", good: "up" },
];

function money(d: Record<string, number> | undefined) {
  const parts = Object.entries(d ?? {}).map(([c, v]) => `${Math.round(v).toLocaleString(LOCALE)} ${c}`);
  return parts.length ? parts.join(", ") : "—";
}

/** Business value: money, customers, pipeline, what the company spends on business vs platform, the owner's time. */
function Business({ p }: { p: Packet }) {
  const b = p.business;
  if (!b) return <p className="p-4 text-sm text-ink-2">{t("reports.biz.none")}</p>;
  const split = b.cost_split;
  const share = split.business_share === null ? "—" : `${Math.round(split.business_share * 100)} %`;
  const rows: [string, string][] = [
    [t("reports.biz.inv_sent"), b.invoices.available ? `${b.invoices.sent ?? 0} · ${money(b.invoices.totals?.sent)}` : "—"],
    [t("reports.biz.inv_received"), b.invoices.available ? `${b.invoices.received ?? 0} · ${money(b.invoices.totals?.received)}` : "—"],
    [t("reports.biz.threads"), String(b.customer_threads.open)],
    [t("reports.biz.pipeline"), t("reports.biz.pipeline_v", { open: b.pipeline.open, new: b.pipeline.new, done: b.pipeline.done })],
    [t("reports.biz.drafts"), t("reports.biz.drafts_v", { total: b.drafts_in_approvals.total, approved: b.drafts_in_approvals.approved })],
    [t("reports.biz.split"), t("reports.biz.split_v", { b: split.business_usd.toFixed(2), p: split.platform_usd.toFixed(2), share })],
    [t("reports.biz.per_outcome"), split.usd_per_business_outcome === null ? "—" : t("reports.biz.per_outcome_v", { usd: split.usd_per_business_outcome.toFixed(2), n: split.business_outcomes })],
    [t("reports.biz.goals"), b.goals.active ? t("reports.biz.goals_v", { n: b.goals.active, avg: b.goals.avg_progress ?? 0 }) : "—"],
  ];
  return (
    <div className="flex flex-col gap-2 p-4 text-[13px]">
      <p className="text-ink">{b.owner_time.line}</p>
      <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1">
        {rows.map(([k, v]) => (
          <Fragment key={k}>
            <dt className="text-ink-2">{k}</dt>
            <dd className="text-right font-mono text-xs break-words text-ink">{v}</dd>
          </Fragment>
        ))}
      </dl>
      {b.invoices.available && <p className="text-xs text-ink-2">{b.invoices.note}</p>}
    </div>
  );
}

function fmtDelta(d: number, fmt?: (v: number) => string) {
  if (fmt) {
    const s = fmt(Math.abs(d));
    return `${d > 0 ? "+" : "−"}${s}`;
  }
  return `${d > 0 ? "+" : "−"}${Math.abs(d)}`;
}

function KpiTile({ label: name, kpi, good, fmt }: { label: string; kpi: Kpi; good: "up" | "down" | "none"; fmt?: (v: number) => string }) {
  const v = kpi.value;
  const d = kpi.delta;
  const better = d === null || d === 0 || good === "none" ? null : (d > 0) === (good === "up");
  const tone = better === null ? "var(--color-ink-2)" : better ? "var(--viz-good)" : "var(--viz-bad)";
  const Icon = d === null || d === 0 ? Minus : d > 0 ? ArrowUpRight : ArrowDownRight;
  return (
    <div className="panel flex min-w-0 flex-col gap-1.5 px-4 py-3">
      <span className="truncate text-xs text-ink-2">{name}</span>
      <span className="font-mono text-[26px] leading-none font-light">{v === null ? "—" : fmt ? fmt(v) : v}</span>
      <span className="flex flex-wrap items-center gap-1 text-xs" style={{ color: tone }}>
        <Icon size={13} aria-hidden />
        {d === null ? <span className="text-ink-2">{t("reports.no_compare")}</span> : d === 0 ? t("reports.no_change") : fmtDelta(d, fmt)}
        {d !== null && kpi.prev !== null && <span className="ml-1 text-ink-2">{t("reports.vs", { prev: fmt ? fmt(kpi.prev) : kpi.prev })}</span>}
      </span>
    </div>
  );
}

function ChartTip({ active, payload, label: name, unit }: TooltipProps<number, string> & { unit?: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-line bg-raised px-3 py-2 text-xs shadow-lg">
      <div className="mb-1 text-ink-2">{name}</div>
      {payload.map((p) => (
        <div key={String(p.dataKey)} className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />
          <span className="text-ink-2">{p.name}</span>
          <span className="ml-auto pl-3 font-mono text-ink">{unit === "$" ? `$${Number(p.value).toFixed(2)}` : p.value}</span>
        </div>
      ))}
    </div>
  );
}

const legendStyle = { fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--color-ink-2)" };

function DoneByDay({ p }: { p: Packet }) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={p.tasks.done_by_day} margin={{ top: 8, right: 8, left: -18, bottom: 0 }} barGap={2}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="day" tickLine={false} axisLine={false} />
        <YAxis allowDecimals={false} tickLine={false} axisLine={false} />
        <Tooltip content={<ChartTip />} cursor={{ fill: "rgb(127 127 127 / 0.08)" }} />
        <ChartLegend wrapperStyle={legendStyle} iconType="circle" iconSize={7} />
        <Bar dataKey="prev" name={t("reports.last_week")} fill="var(--viz-prev)" radius={[4, 4, 0, 0]} maxBarSize={14} />
        <Bar dataKey="done" name={t("reports.this_week")} fill="var(--viz-series-1)" radius={[4, 4, 0, 0]} maxBarSize={14} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function ByProject({ p }: { p: Packet }) {
  const data = p.tasks.by_project.filter((x) => x.done || x.open).map((x) => ({ ...x, name: x.name === "—" ? t("reports.no_project") : x.name }));
  if (!data.length) return <p className="px-4 py-6 text-xs text-ink-2">{t("reports.no_project_work")}</p>;
  return (
    <ResponsiveContainer width="100%" height={Math.max(160, data.length * 34 + 50)}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 0 }} barGap={2}>
        <CartesianGrid horizontal={false} />
        <XAxis type="number" allowDecimals={false} tickLine={false} axisLine={false} />
        <YAxis
          type="category"
          dataKey="name"
          width={120}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: string) => (v.length > 17 ? `${v.slice(0, 16)}…` : v)}
        />
        <Tooltip content={<ChartTip />} cursor={{ fill: "rgb(127 127 127 / 0.08)" }} />
        <ChartLegend wrapperStyle={legendStyle} iconType="circle" iconSize={7} />
        <Bar dataKey="done" name={t("reports.done")} fill="var(--viz-series-1)" radius={[0, 4, 4, 0]} maxBarSize={12} />
        <Bar dataKey="open" name={t("reports.open")} fill="var(--viz-series-2)" radius={[0, 4, 4, 0]} maxBarSize={12} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function AgentCost({ p }: { p: Packet }) {
  const [archived, setArchived] = useState(false);
  const all = p.agents.per_agent;
  const archivedCount = all.filter((r) => r.archived).length;
  const rows = archived ? all : all.filter((r) => !r.archived);
  if (!all.length) return <p className="px-4 py-6 text-xs text-ink-2">{t("reports.agents_idle")}</p>;
  const max = Math.max(...rows.map((r) => r.cost_usd), 0.01);
  const GRID = "grid grid-cols-[minmax(0,1.2fr)_minmax(0,2fr)_56px_64px] gap-3";
  return (
    <div className="flex min-w-0 flex-col">
      <div className={`${GRID} border-b border-line px-4 py-2 text-xs text-ink-2`}>
        <span>{t("reports.h.agent")}</span>
        <span>{t("reports.h.cost")}</span>
        <span className="text-right">{t("reports.h.success")}</span>
        <span className="text-right">{t("reports.h.per_task")}</span>
      </div>
      {rows.length === 0 && <p className="px-4 py-3 text-xs text-ink-2">{t("reports.only_archived")}</p>}
      {rows.map((r) => {
        const rate = r.success_rate;
        const tone = rate === null ? "var(--color-ink-2)" : rate >= 0.9 ? "var(--viz-good)" : rate >= 0.7 ? "var(--viz-warn)" : "var(--viz-bad)";
        return (
          <div
            key={r.id ?? r.name}
            className={`${GRID} items-center border-b border-line px-4 py-2 text-[13px] last:border-0`}
            title={t("reports.agent_title", { runs: r.runs, ok: r.ok, errors: r.errors, accepted: r.accepted, handbacks: r.handbacks, tokens: r.tokens.toLocaleString(LOCALE) })}
          >
            <span className="flex min-w-0 items-center gap-1.5">
              <span className="truncate">{r.name}</span>
              {r.archived && <span className="shrink-0 rounded border border-line px-1 text-xs text-ink-2">{t("status.archived")}</span>}
            </span>
            <span className="flex min-w-0 items-center gap-2">
              <span className="h-2.5 rounded-r" style={{ width: `${Math.max(2, (r.cost_usd / max) * 100)}%`, background: "var(--viz-series-1)" }} />
              <span className="font-mono text-xs text-ink-2">${r.cost_usd.toFixed(2)}</span>
            </span>
            <span className="text-right font-mono text-xs" style={{ color: tone }}>
              {rate === null ? "—" : `${Math.round(rate * 100)} %`}
            </span>
            <span className="text-right font-mono text-xs text-ink-2">{r.cost_per_accepted === null ? "—" : `$${r.cost_per_accepted.toFixed(2)}`}</span>
          </div>
        );
      })}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-xs text-ink-2">
        <span>{t("reports.agents_total", { runs: p.agents.runs, accepted: p.agents.accepted, handbacks: p.agents.handbacks })}</span>
        {archivedCount > 0 && (
          <label className="no-print ml-auto flex items-center gap-1.5">
            <input type="checkbox" checked={archived} onChange={(e) => setArchived(e.target.checked)} />
            {t("act.show_archived")} ({archivedCount})
          </label>
        )}
      </div>
    </div>
  );
}

function GoalBars({ goals }: { goals: PacketGoal[] }) {
  if (!goals.length) return <p className="px-4 py-6 text-xs text-ink-2">{t("reports.no_goals")}</p>;
  return (
    <div className="flex flex-col gap-3.5 p-4">
      {goals.map((g) => (
        <div key={g.id} className={`flex min-w-0 flex-col gap-1.5 ${g.parent_id ? "pl-4" : ""}`}>
          <div className="flex items-baseline gap-2 text-[13px]">
            <span className="min-w-0 flex-1 truncate">{g.title}</span>
            {g.delta != null && g.delta !== 0 && (
              <span className="font-mono text-xs" style={{ color: g.delta > 0 ? "var(--viz-good)" : "var(--viz-bad)" }}>
                {g.delta > 0 ? "+" : ""}
                {g.delta} b.
              </span>
            )}
            <span className="font-mono text-xs">{g.progress} %</span>
          </div>
          <div
            className="h-2 overflow-hidden rounded-full bg-raised"
            role="progressbar"
            aria-valuenow={g.progress}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={g.title}
          >
            <div className="h-full rounded-full" style={{ width: `${g.progress}%`, background: g.status === "paused" ? "var(--viz-prev)" : "var(--viz-series-3)" }} />
          </div>
          <span className="truncate text-xs text-ink-2">
            {g.target || t("reports.no_target")}
            {g.owner ? ` · ${g.owner}` : ""}
            {g.due ? t("reports.due", { due: g.due }) : ""}
            {g.tasks_total ? t("reports.goal_tasks", { done: g.tasks_done, total: g.tasks_total }) : ""}
            {g.status !== "active" ? ` · ${label("reports.goal", g.status)}` : ""}
          </span>
        </div>
      ))}
    </div>
  );
}

function TaskList({ items, empty, extra }: { items: { ref: string; title: string; assignee?: string | null }[]; empty: string; extra?: (i: number) => ReactNode }) {
  if (!items.length) return <p className="px-4 py-3 text-xs text-ink-2">{empty}</p>;
  return (
    <>
      {items.map((x, i) => (
        <TaskLink
          key={x.ref + i}
          taskRef={x.ref}
          className="flex min-w-0 items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0 hover:bg-raised"
        >
          <span className="shrink-0 font-mono text-xs text-ink-2">{x.ref}</span>
          <span className="min-w-0 flex-1 truncate">{x.title}</span>
          {extra?.(i)}
          {x.assignee && <span className="max-w-[35%] truncate text-xs text-ink-2">{x.assignee}</span>}
        </TaskLink>
      ))}
    </>
  );
}

function Heading({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`text-xs font-medium text-ink-2 ${className}`}>{children}</span>;
}

function Signals({ p }: { p: Packet }) {
  const inc = p.incidents;
  const dev = p.dev;
  const comm = p.communication;
  return (
    <div className="flex min-w-0 flex-col gap-3 p-4 text-[13px]">
      <div className="flex flex-col gap-1">
        <Heading>{t("reports.dev")}</Heading>
        {dev.available ? (
          <>
            <span>
              {t("reports.dev_line", { commits: dev.commits, ok: dev.deploys.ok ?? 0 })}
              {dev.merges ? t("reports.dev_merges", { n: dev.merges }) : ""}
              {dev.deploys.reverted ? t("reports.dev_reverted", { n: dev.deploys.reverted }) : ""}
            </span>
            {dev.repos.slice(0, 6).map((r) => (
              <span key={r.repo} className="flex gap-2 text-ink-2">
                <span className="min-w-0 flex-1 truncate font-mono text-xs">{r.repo}</span>
                <span className="font-mono text-xs">{r.commits}</span>
              </span>
            ))}
          </>
        ) : (
          <span className="break-words text-ink-2">
            {t("reports.deploys_ok", { n: dev.deploys?.ok ?? 0 })} · <span className="text-xs">{dev.note}</span>
          </span>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <Heading>{t("reports.comm")}</Heading>
        {comm.available ? (
          <>
            <span className="break-words">
              {t("reports.comm_items", { n: comm.items ?? 0 })}{" "}
              {Object.entries(comm.by_origin ?? {})
                .map(([k, v]) => `${k} ${v}`)
                .join(" · ")}
            </span>
            {(comm.top_channels ?? []).slice(0, 4).map((c) => (
              <span key={c.origin + c.channel} className="flex gap-2 text-ink-2">
                <span className="min-w-0 flex-1 truncate">{c.channel}</span>
                <span className="text-xs">{c.origin}</span>
                <span className="font-mono text-xs">{c.items}</span>
              </span>
            ))}
          </>
        ) : (
          <span className="text-xs break-words text-ink-2">{comm.note}</span>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <Heading>{t("reports.incidents")}</Heading>
        <span className={inc.failed_deploys.length || inc.failed_runs ? "" : "text-ink-2"}>
          {t("reports.incidents_line", { deploys: inc.failed_deploys.length, runs: inc.failed_runs, freezes: inc.freezes })}
          {inc.engine_limit_hits ? t("reports.limit_hits", { n: inc.engine_limit_hits }) : ""}
        </span>
        {inc.failed_deploys.map((d, i) => (
          <span key={i} className="text-xs break-words text-ink-2">
            {t("reports.failed_deploy", { at: d.at.replace("T", " "), status: label("sys.deploy", d.status), stage: d.stage || "?", author: d.author || "?", sha: d.sha })}
          </span>
        ))}
      </div>
    </div>
  );
}

function Meeting({ r }: { r: Report }) {
  const [open, setOpen] = useState(false);
  if (r.status === "draft" || r.status === "published") return null;
  return (
    <Panel title={t("reports.meeting")} right={statusWord(r.status)} className="min-w-0 lg:col-span-2">
      <div className="grid grid-cols-1 gap-5 p-4 lg:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-3">
          {r.meeting_notes ? <Markdown text={r.meeting_notes} /> : <p className="text-xs text-ink-2">{t("reports.notes_later")}</p>}
          {r.questions.length > 0 && (
            <div className="flex flex-col gap-1">
              <Heading>{t("reports.questions")}</Heading>
              <ol className="list-decimal pl-5 text-[13px] text-ink-2">
                {r.questions.map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ol>
            </div>
          )}
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <div className="flex flex-col">
            <Heading className="mb-1">{t("reports.next_tasks")}</Heading>
            <div className="rounded border border-line">
              <TaskList items={r.tasks_created} empty={t("reports.no_new_tasks")} />
            </div>
          </div>
          {r.goals_changed.length > 0 && (
            <div className="flex flex-col">
              <Heading className="mb-1">{t("reports.goals_changed")}</Heading>
              <div className="rounded border border-line">
                <GoalBars goals={r.goals_changed} />
              </div>
            </div>
          )}
          {r.transcript.length > 0 && (
            <div className="no-print">
              <button className="btn" onClick={() => setOpen((v) => !v)}>
                {open ? t("reports.hide_chat") : t("reports.show_chat", { n: r.transcript.length })}
              </button>
              {open && (
                <div className="mt-2 flex flex-col gap-2">
                  {r.transcript.map((m) => (
                    <div key={m.id} className={`rounded border border-line p-2.5 ${m.owner ? "bg-raised" : ""}`}>
                      <span className="text-xs text-ink-2">
                        {m.author} · {m.at.slice(0, 16).replace("T", " ")}
                      </span>
                      <Markdown text={m.body} compact />
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </Panel>
  );
}

function ReportView({ r }: { r: Report }) {
  const p = r.packet;
  const kpis = KPIS.filter((k) => p.kpis?.[k.key]);
  return (
    <div className="report flex min-w-0 flex-col gap-4">
      {r.headline && <p className="text-lg font-light break-words text-ink">{r.headline}</p>}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {kpis.map((k) => (
          <KpiTile key={k.key} label={t(`reports.kpi.${k.key}`)} kpi={p.kpis[k.key]} good={k.good} fmt={k.fmt} />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title={t("reports.done_by_day")} right={t("reports.done_by_day_right", { done: p.tasks.done, new: p.tasks.new })} className="min-w-0">
          <div className="px-2 pt-3 pb-1">
            <DoneByDay p={p} />
          </div>
        </Panel>
        <Panel title={t("reports.by_project")} right={t("reports.by_project_right")} className="min-w-0">
          <div className="px-2 pt-3 pb-1">
            <ByProject p={p} />
          </div>
        </Panel>
        <Panel title={t("reports.summary")} right={r.narrative ? t("reports.written_by") : t("reports.waiting_agent")} className="min-w-0 lg:row-span-2">
          <div className="p-4">
            {r.narrative ? <Markdown text={r.narrative} /> : <p className="text-xs text-ink-2">{t("reports.no_narrative")}</p>}
            {r.decisions.length > 0 && (
              <div className="mt-4 rounded border border-amber-400/50 bg-amber-300/5 p-3">
                <span className="text-xs font-medium text-amber-300">{t("reports.decisions")}</span>
                <ul className="mt-1.5 list-disc pl-5 text-[13px]">
                  {r.decisions.map((d) => (
                    <li key={d}>{d}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </Panel>
        <Panel title={t("reports.goals")} right={t("reports.goals_right")} className="min-w-0">
          <GoalBars goals={p.goals} />
        </Panel>
        <Panel
          title={t("reports.agents")}
          right={`$${p.agents.cost_usd.toFixed(2)} · ${p.agents.success_rate === null ? "—" : Math.round(p.agents.success_rate * 100) + " %"}`}
          className="min-w-0"
        >
          <AgentCost p={p} />
        </Panel>
        <Panel title={t("reports.signals")} className="min-w-0">
          <Signals p={p} />
        </Panel>
        <Panel title={t("reports.biz.title")} right={p.business ? t("reports.biz.right", { usd: p.business.cost_split.business_usd.toFixed(2) }) : undefined} className="min-w-0">
          <Business p={p} />
        </Panel>
        <Panel title={t("reports.highlights")} right={`${p.tasks.highlights.length}`} className="min-w-0">
          <TaskList
            items={p.tasks.highlights}
            empty={t("reports.nothing_done")}
            extra={(i) => {
              const h = p.tasks.highlights[i];
              return h.project ? <span className="max-w-[30%] truncate text-xs text-ink-2">{h.project}</span> : null;
            }}
          />
        </Panel>
        <Panel title={t("reports.stuck")} right={t("reports.stuck_right", { waiting: p.tasks.waiting, overdue: p.tasks.overdue })} className="min-w-0">
          <TaskList
            items={[...p.tasks.overdue_list, ...p.tasks.waiting_list]}
            empty={t("reports.nothing_stuck")}
            extra={(i) => {
              const all = [
                ...p.tasks.overdue_list.map((x) => t("reports.overdue_since", { deadline: x.deadline ?? "" })),
                ...p.tasks.waiting_list.map((x) => t("reports.waiting_since", { since: x.since })),
              ];
              return <span className="shrink-0 text-xs text-ink-2">{all[i]}</span>;
            }}
          />
        </Panel>
        {p.last_meeting && (
          <Panel
            title={t("reports.last_meeting", { week: p.last_meeting.week })}
            right={t("reports.last_meeting_right", { done: p.last_meeting.tasks_done, n: p.last_meeting.tasks.length })}
            className="min-w-0 lg:col-span-2"
          >
            <TaskList
              items={p.last_meeting.tasks}
              empty={t("reports.last_meeting_none")}
              extra={(i) => <span className="text-xs text-ink-2">{label("task", p.last_meeting!.tasks[i].status ?? "")}</span>}
            />
          </Panel>
        )}
        <Meeting r={r} />
      </div>
      <p className="text-xs break-words text-ink-2">
        {t("reports.footer", { at: p.generated_at.slice(0, 16).replace("T", " ") })}
        {p.period.partial ? t("reports.partial") : ""}
      </p>
    </div>
  );
}

/** Weekly reports by the Chief of Staff, browsable by week and printable. */
export default function Reports() {
  const { week } = useParams();
  const navigate = useNavigate();
  const [list, setList] = useState<ReportListItem[] | null>(null);
  const [current, setCurrent] = useState<string>("");
  const [schedule, setSchedule] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadList = useCallback(
    () =>
      reportsApi.list().then((l) => {
        setList(l.reports);
        setCurrent(l.current_week);
        setSchedule(l.schedule);
        return l;
      }),
    [],
  );

  useEffect(() => {
    loadList().then(
      (l) => {
        if (!week && l.reports.length) navigate(`/reports/${l.reports[0].week}`, { replace: true });
      },
      (e) => setError(e.message),
    );
  }, [loadList, week, navigate]);

  useEffect(() => {
    if (!week) return;
    setReport(null);
    setError(null);
    reportsApi.get(week).then(setReport, (e) => setError(e.status === 404 ? null : e.message));
  }, [week]);

  const weeks = useMemo(() => (list ?? []).map((r) => r.week), [list]);
  const idx = week ? weeks.indexOf(week) : -1;
  const older = idx >= 0 && idx < weeks.length - 1 ? weeks[idx + 1] : null;
  const newer = idx > 0 ? weeks[idx - 1] : null;

  async function build(w: string) {
    setBusy(true);
    try {
      await reportsApi.build(w);
      await loadList();
      if (w === week) setReport(await reportsApi.get(w));
      else navigate(`/reports/${w}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const period = report ? `${report.period_start} – ${report.period_end}` : "";
  return (
    <div className="flex min-w-0 flex-col gap-5">
      <PageHeader
        kicker={t("settings.kicker")}
        title={week ? t("reports.week", { week }) : t("nav.reports")}
        sub={
          report ? (
            <>
              {period} · <span className={STATUS_TONE[report.status]}>{statusWord(report.status)}</span>
            </>
          ) : schedule ? (
            t("reports.schedule", { schedule })
          ) : (
            t("reports.sub")
          )
        }
      />
      <div className="no-print flex flex-wrap items-center gap-2">
        <button className="btn" disabled={!older} onClick={() => older && navigate(`/reports/${older}`)} aria-label={t("reports.older")}>
          <ChevronLeft size={14} />
        </button>
        <select
          className="h-8 min-w-0 rounded border border-line bg-bg px-2 text-[13px]"
          value={week ?? ""}
          onChange={(e) => navigate(`/reports/${e.target.value}`)}
          aria-label={t("reports.week_label")}
        >
          {!week && <option value="">{t("reports.pick_week")}</option>}
          {week && !weeks.includes(week) && <option value={week}>{week}</option>}
          {(list ?? []).map((r) => (
            <option key={r.week} value={r.week}>
              {r.week} · {statusWord(r.status)}
            </option>
          ))}
        </select>
        <button className="btn" disabled={!newer} onClick={() => newer && navigate(`/reports/${newer}`)} aria-label={t("reports.newer")}>
          <ChevronRight size={14} />
        </button>
        {current && !weeks.includes(current) && (
          <button className="btn" disabled={busy} onClick={() => build(current)} title={t("reports.preview_title")}>
            <RefreshCw size={14} /> {t("reports.preview", { week: current })}
          </button>
        )}
        {report?.status === "draft" && (
          <button className="btn" disabled={busy} onClick={() => build(report.week)} title={t("reports.rebuild_title")}>
            <RefreshCw size={14} /> {t("reports.rebuild")}
          </button>
        )}
        {report && (
          <button className="btn ml-auto" onClick={() => window.print()}>
            <Printer size={14} /> {t("reports.print")}
          </button>
        )}
      </div>
      {error && <p className="text-xs break-words text-red-400">{error}</p>}
      {list && !list.length && !week && (
        <Panel title={t("reports.none_title")}>
          <p className="p-4 text-sm text-ink-2">
            {schedule ? t("reports.none_body_schedule", { schedule }) : t("reports.none_body")}
          </p>
        </Panel>
      )}
      {week && !report && !error && list && !weeks.includes(week) && <p className="text-xs text-ink-2">{t("reports.no_report", { week })}</p>}
      {report && <ReportView r={report} />}
    </div>
  );
}
