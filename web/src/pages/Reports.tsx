import { ArrowDownRight, ArrowUpRight, ChevronLeft, ChevronRight, Minus, Printer, RefreshCw } from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
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
import { type Kpi, type Packet, type PacketGoal, type Report, type ReportListItem, type ReportStatus, reportsApi } from "../reportsApi";

const STATUS: Record<ReportStatus, [string, string]> = {
  draft: ["koncept", "text-ink-3"],
  meeting: ["meeting běží", "text-amber-300"],
  published: ["publikováno", "text-accent"],
  closed: ["uzavřeno", "text-accent"],
  no_reply: ["bez meetingu", "text-ink-2"],
};

/** Which way is good for a KPI: up (more done), down (less waiting), or neutral. */
const KPIS: { key: string; label: string; good: "up" | "down" | "none"; fmt?: (v: number) => string }[] = [
  { key: "tasks_done", label: "Hotovo", good: "up" },
  { key: "tasks_new", label: "Nové úkoly", good: "none" },
  { key: "waiting", label: "Čeká", good: "down" },
  { key: "overdue", label: "Po termínu", good: "down" },
  { key: "agent_success_rate", label: "Úspěšnost agentů", good: "up", fmt: (v) => `${Math.round(v * 100)} %` },
  { key: "agent_cost_usd", label: "Náklady agentů", good: "down", fmt: (v) => `$${v.toFixed(2)}` },
  { key: "commits", label: "Commity", good: "up" },
  { key: "deploys", label: "Deploye", good: "up" },
  { key: "communication", label: "Komunikace", good: "none" },
  { key: "business_outcomes", label: "Byznys výsledky", good: "up" },
  { key: "usd_per_business_outcome", label: "$ za byznys výsledek", good: "down", fmt: (v) => `$${v.toFixed(2)}` },
  { key: "owner_minutes", label: "Čas majitele (min)", good: "down" },
  { key: "customer_threads_open", label: "Otevřená zákaznická vlákna", good: "down" },
  { key: "drafts_in_approvals", label: "Koncepty ke schválení", good: "up" },
  { key: "invoices_sent", label: "Vydané faktury", good: "up" },
];

function money(d: Record<string, number> | undefined) {
  const parts = Object.entries(d ?? {}).map(([c, v]) => `${Math.round(v).toLocaleString("cs-CZ")} ${c}`);
  return parts.length ? parts.join(", ") : "—";
}

/** Business value: money, customers, pipeline, what the company spends on business vs platform, the owner's time. */
function Business({ p }: { p: Packet }) {
  const b = p.business;
  if (!b) return <p className="cap p-4">Tento report ještě nemá byznys čísla.</p>;
  const split = b.cost_split;
  const share = split.business_share === null ? "—" : `${Math.round(split.business_share * 100)} %`;
  const rows: [string, string][] = [
    ["Faktury vydané", b.invoices.available ? `${b.invoices.sent ?? 0} · ${money(b.invoices.totals?.sent)}` : "—"],
    ["Faktury přijaté", b.invoices.available ? `${b.invoices.received ?? 0} · ${money(b.invoices.totals?.received)}` : "—"],
    ["Zákaznická vlákna otevřená", String(b.customer_threads.open)],
    ["Pipeline (Growth)", `otevřeno ${b.pipeline.open} · nové ${b.pipeline.new} · uzavřeno ${b.pipeline.done}`],
    ["Koncepty ke schválení", `${b.drafts_in_approvals.total} (schváleno ${b.drafts_in_approvals.approved})`],
    ["Byznys / platforma", `$${split.business_usd.toFixed(2)} / $${split.platform_usd.toFixed(2)} · byznys ${share}`],
    ["$ za byznys výsledek", split.usd_per_business_outcome === null ? "—" : `$${split.usd_per_business_outcome.toFixed(2)} (${split.business_outcomes} výsledků)`],
    ["Cíle", b.goals.active ? `${b.goals.active} aktivních · průměr ${b.goals.avg_progress ?? 0} %` : "—"],
  ];
  return (
    <div className="flex flex-col gap-2 p-4 text-[13px]">
      <p className="text-ink">{b.owner_time.line}</p>
      <dl className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1">
        {rows.map(([k, v]) => (
          <Fragment key={k}>
            <dt className="text-ink-2">{k}</dt>
            <dd className="text-right font-mono text-xs text-ink">{v}</dd>
          </Fragment>
        ))}
      </dl>
      {b.invoices.available && <p className="cap">{b.invoices.note}</p>}
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

function KpiTile({ label, kpi, good, fmt }: { label: string; kpi: Kpi; good: "up" | "down" | "none"; fmt?: (v: number) => string }) {
  const v = kpi.value;
  const d = kpi.delta;
  const better = d === null || d === 0 || good === "none" ? null : (d > 0) === (good === "up");
  const tone = better === null ? "var(--color-ink-3)" : better ? "var(--viz-good)" : "var(--viz-bad)";
  const Icon = d === null || d === 0 ? Minus : d > 0 ? ArrowUpRight : ArrowDownRight;
  return (
    <div className="panel flex min-w-0 flex-col gap-1.5 px-4 py-3">
      <span className="cap truncate">{label.toUpperCase()}</span>
      <span className="font-mono text-[26px] leading-none font-light">{v === null ? "—" : fmt ? fmt(v) : v}</span>
      <span className="flex items-center gap-1 text-xs" style={{ color: tone }}>
        <Icon size={13} aria-hidden />
        {d === null ? <span className="text-ink-3">bez srovnání</span> : d === 0 ? "beze změny" : fmtDelta(d, fmt)}
        {d !== null && kpi.prev !== null && (
          <span className="cap ml-1">vs {fmt ? fmt(kpi.prev) : kpi.prev}</span>
        )}
      </span>
    </div>
  );
}

function ChartTip({ active, payload, label, unit }: TooltipProps<number, string> & { unit?: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-line bg-raised px-3 py-2 text-xs shadow-lg">
      <div className="cap mb-1">{label}</div>
      {payload.map((p) => (
        <div key={String(p.dataKey)} className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />
          <span className="text-ink-2">{p.name}</span>
          <span className="ml-auto pl-3 font-mono text-ink">
            {unit === "$" ? `$${Number(p.value).toFixed(2)}` : p.value}
          </span>
        </div>
      ))}
    </div>
  );
}

const legendStyle = { fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--color-ink-2)" };

function DoneByDay({ p }: { p: Packet }) {
  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={p.tasks.done_by_day} margin={{ top: 8, right: 8, left: -18, bottom: 0 }} barGap={2}>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="day" tickLine={false} axisLine={false} />
        <YAxis allowDecimals={false} tickLine={false} axisLine={false} />
        <Tooltip content={<ChartTip />} cursor={{ fill: "rgb(127 127 127 / 0.08)" }} />
        <ChartLegend wrapperStyle={legendStyle} iconType="circle" iconSize={7} />
        <Bar dataKey="prev" name="minulý týden" fill="var(--viz-prev)" radius={[4, 4, 0, 0]} maxBarSize={14} />
        <Bar dataKey="done" name="tento týden" fill="var(--viz-series-1)" radius={[4, 4, 0, 0]} maxBarSize={14} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function ByProject({ p }: { p: Packet }) {
  const data = p.tasks.by_project
    .filter((x) => x.done || x.open)
    .map((x) => ({ ...x, name: x.name === "—" ? "bez projektu" : x.name }));
  if (!data.length) return <p className="cap px-4 py-6">Žádná práce v projektech.</p>;
  return (
    <ResponsiveContainer width="100%" height={Math.max(160, data.length * 34 + 50)}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 0 }} barGap={2}>
        <CartesianGrid horizontal={false} />
        <XAxis type="number" allowDecimals={false} tickLine={false} axisLine={false} />
        <YAxis type="category" dataKey="name" width={120} tickLine={false} axisLine={false}
          tickFormatter={(v: string) => (v.length > 17 ? `${v.slice(0, 16)}…` : v)} />
        <Tooltip content={<ChartTip />} cursor={{ fill: "rgb(127 127 127 / 0.08)" }} />
        <ChartLegend wrapperStyle={legendStyle} iconType="circle" iconSize={7} />
        <Bar dataKey="done" name="hotovo" fill="var(--viz-series-1)" radius={[0, 4, 4, 0]} maxBarSize={12} />
        <Bar dataKey="open" name="otevřené" fill="var(--viz-series-2)" radius={[0, 4, 4, 0]} maxBarSize={12} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function AgentCost({ p }: { p: Packet }) {
  const rows = p.agents.per_agent;
  if (!rows.length) return <p className="cap px-4 py-6">Agenti tento týden nepracovali.</p>;
  const max = Math.max(...rows.map((r) => r.cost_usd), 0.01);
  return (
    <div className="flex flex-col">
      <div className="cap grid grid-cols-[minmax(0,1.2fr)_minmax(0,2fr)_56px_64px] gap-3 border-b border-line px-4 py-2">
        <span>AGENT</span>
        <span>NÁKLADY</span>
        <span className="text-right">ÚSPĚCH</span>
        <span className="text-right">$/ÚKOL</span>
      </div>
      {rows.map((r) => {
        const rate = r.success_rate;
        const tone = rate === null ? "var(--color-ink-3)" : rate >= 0.9 ? "var(--viz-good)" : rate >= 0.7 ? "var(--viz-warn)" : "var(--viz-bad)";
        return (
          <div key={r.name} className="grid grid-cols-[minmax(0,1.2fr)_minmax(0,2fr)_56px_64px] items-center gap-3 border-b border-line px-4 py-2 text-[13px] last:border-0"
            title={`${r.runs} běhů (${r.ok} ok, ${r.errors} chyb) · ${r.accepted} přijatých úkolů · ${r.handbacks} vrácených · ${r.tokens.toLocaleString("cs-CZ")} tokenů`}>
            <span className="truncate">{r.name}</span>
            <span className="flex items-center gap-2">
              <span className="h-2.5 rounded-r" style={{ width: `${Math.max(2, (r.cost_usd / max) * 100)}%`, background: "var(--viz-series-1)" }} />
              <span className="font-mono text-xs text-ink-2">${r.cost_usd.toFixed(2)}</span>
            </span>
            <span className="text-right font-mono text-xs" style={{ color: tone }}>
              {rate === null ? "—" : `${Math.round(rate * 100)} %`}
            </span>
            <span className="text-right font-mono text-xs text-ink-2">
              {r.cost_per_accepted === null ? "—" : `$${r.cost_per_accepted.toFixed(2)}`}
            </span>
          </div>
        );
      })}
      <p className="cap px-4 py-2">
        celkem {p.agents.runs} běhů · {p.agents.accepted} přijatých úkolů · {p.agents.handbacks} vrácených · Codex jen v tokenech
      </p>
    </div>
  );
}

function GoalBars({ goals }: { goals: PacketGoal[] }) {
  if (!goals.length) return <p className="cap px-4 py-6">Zatím žádné cíle. Vzniknou na týdenním meetingu.</p>;
  return (
    <div className="flex flex-col gap-3.5 p-4">
      {goals.map((g) => (
        <div key={g.id} className={`flex flex-col gap-1.5 ${g.parent_id ? "pl-4" : ""}`}>
          <div className="flex items-baseline gap-2 text-[13px]">
            <span className="min-w-0 flex-1 truncate">{g.title}</span>
            {g.delta != null && g.delta !== 0 && (
              <span className="font-mono text-xs" style={{ color: g.delta > 0 ? "var(--viz-good)" : "var(--viz-bad)" }}>
                {g.delta > 0 ? "+" : ""}{g.delta} b.
              </span>
            )}
            <span className="font-mono text-xs">{g.progress} %</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-raised" role="progressbar" aria-valuenow={g.progress} aria-valuemin={0} aria-valuemax={100} aria-label={g.title}>
            <div className="h-full rounded-full" style={{ width: `${g.progress}%`, background: g.status === "paused" ? "var(--viz-prev)" : "var(--viz-series-3)" }} />
          </div>
          <span className="cap truncate">
            {g.target || "bez měřitelného cíle"}
            {g.owner ? ` · ${g.owner}` : ""}
            {g.due ? ` · do ${g.due}` : ""}
            {g.tasks_total ? ` · úkoly ${g.tasks_done}/${g.tasks_total}` : ""}
            {g.status !== "active" ? ` · ${g.status}` : ""}
          </span>
        </div>
      ))}
    </div>
  );
}

function TaskList({ items, empty, extra }: { items: { ref: string; title: string; assignee?: string | null }[]; empty: string; extra?: (i: number) => ReactNode }) {
  if (!items.length) return <p className="cap px-4 py-3">{empty}</p>;
  return (
    <>
      {items.map((t, i) => (
        <Link key={t.ref + i} to={`/tasks?task=${t.ref}`} className="flex items-center gap-2 border-b border-line px-4 py-2 text-[13px] last:border-0 hover:bg-raised">
          <span className="cap">{t.ref}</span>
          <span className="min-w-0 flex-1 truncate">{t.title}</span>
          {extra?.(i)}
          {t.assignee && <span className="cap truncate">{t.assignee}</span>}
        </Link>
      ))}
    </>
  );
}

function Signals({ p }: { p: Packet }) {
  const inc = p.incidents;
  const dev = p.dev;
  const comm = p.communication;
  return (
    <div className="flex flex-col gap-3 p-4 text-[13px]">
      <div className="flex flex-col gap-1">
        <span className="cap">VÝVOJ</span>
        {dev.available ? (
          <>
            <span>
              {dev.commits} commitů{dev.merges ? `, ${dev.merges} merge` : ""} · deploye {dev.deploys.ok ?? 0} ok
              {dev.deploys.reverted ? `, ${dev.deploys.reverted} vráceno` : ""}
            </span>
            {dev.repos.slice(0, 6).map((r) => (
              <span key={r.repo} className="flex gap-2 text-ink-2">
                <span className="min-w-0 flex-1 truncate font-mono text-xs">{r.repo}</span>
                <span className="font-mono text-xs">{r.commits}</span>
              </span>
            ))}
          </>
        ) : (
          <span className="text-ink-2">
            deploye {dev.deploys?.ok ?? 0} ok · <span className="cap">{dev.note}</span>
          </span>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <span className="cap">KOMUNIKACE</span>
        {comm.available ? (
          <>
            <span>
              {comm.items} nových položek ·{" "}
              {Object.entries(comm.by_origin ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ")}
            </span>
            {(comm.top_channels ?? []).slice(0, 4).map((c) => (
              <span key={c.origin + c.channel} className="flex gap-2 text-ink-2">
                <span className="min-w-0 flex-1 truncate">{c.channel}</span>
                <span className="cap">{c.origin}</span>
                <span className="font-mono text-xs">{c.items}</span>
              </span>
            ))}
          </>
        ) : (
          <span className="cap">{comm.note}</span>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <span className="cap">INCIDENTY</span>
        <span className={inc.failed_deploys.length || inc.failed_runs ? "" : "text-ink-2"}>
          {inc.failed_deploys.length} vadných deployů · {inc.failed_runs} chybných běhů · {inc.freezes}× kill switch
          {inc.engine_limit_hits ? ` · ${inc.engine_limit_hits}× limit předplatného` : ""}
        </span>
        {inc.failed_deploys.map((d, i) => (
          <span key={i} className="cap">
            {d.at.replace("T", " ")} UTC · {d.status} ve fázi {d.stage || "?"} · {d.author || "?"} · {d.sha}
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
    <Panel title="Týdenní meeting" right={STATUS[r.status][0]} className="lg:col-span-2">
      <div className="grid grid-cols-1 gap-5 p-4 lg:grid-cols-2">
        <div className="flex flex-col gap-3">
          {r.meeting_notes ? <Markdown text={r.meeting_notes} /> : <p className="cap">Zápis vznikne po meetingu.</p>}
          {r.questions.length > 0 && (
            <div className="flex flex-col gap-1">
              <span className="cap">OTÁZKY</span>
              <ol className="list-decimal pl-5 text-[13px] text-ink-2">
                {r.questions.map((q) => <li key={q}>{q}</li>)}
              </ol>
            </div>
          )}
        </div>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col">
            <span className="cap mb-1">ÚKOLY NA PŘÍŠTÍ TÝDEN</span>
            <div className="rounded border border-line">
              <TaskList items={r.tasks_created} empty="Žádné nové úkoly." />
            </div>
          </div>
          {r.goals_changed.length > 0 && (
            <div className="flex flex-col">
              <span className="cap mb-1">CÍLE NOVÉ NEBO ZMĚNĚNÉ</span>
              <div className="rounded border border-line"><GoalBars goals={r.goals_changed} /></div>
            </div>
          )}
          {r.transcript.length > 0 && (
            <div className="no-print">
              <button className="btn" onClick={() => setOpen((v) => !v)}>
                {open ? "Skrýt konverzaci" : `Konverzace v #weekly (${r.transcript.length})`}
              </button>
              {open && (
                <div className="mt-2 flex flex-col gap-2">
                  {r.transcript.map((m) => (
                    <div key={m.id} className={`rounded border border-line p-2.5 ${m.owner ? "bg-raised" : ""}`}>
                      <span className="cap">{m.author} · {m.at.slice(0, 16).replace("T", " ")}</span>
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
    <div className="report flex flex-col gap-4">
      {r.headline && <p className="text-lg font-light text-ink">{r.headline}</p>}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {kpis.map((k) => <KpiTile key={k.key} label={k.label} kpi={p.kpis[k.key]} good={k.good} fmt={k.fmt} />)}
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Hotové úkoly po dnech" right={`${p.tasks.done} hotovo · ${p.tasks.new} nových`}>
          <div className="px-2 pt-3 pb-1"><DoneByDay p={p} /></div>
        </Panel>
        <Panel title="Práce podle projektu" right="hotovo tento týden · otevřené teď">
          <div className="px-2 pt-3 pb-1"><ByProject p={p} /></div>
        </Panel>
        <Panel title="Shrnutí týdne" right={r.narrative ? "napsal Asistent vedení" : "čeká na agenta"} className="lg:row-span-2">
          <div className="p-4">
            {r.narrative ? <Markdown text={r.narrative} /> : <p className="cap">Narativ ještě není napsaný; čísla jsou už spočítaná.</p>}
            {r.decisions.length > 0 && (
              <div className="mt-4 rounded border border-amber-400/50 bg-amber-300/5 p-3">
                <span className="cap text-amber-300!">ROZHODNUTÍ PRO TEBE</span>
                <ul className="mt-1.5 list-disc pl-5 text-[13px]">
                  {r.decisions.map((d) => <li key={d}>{d}</li>)}
                </ul>
              </div>
            )}
          </div>
        </Panel>
        <Panel title="Cíle" right="postup a změna od minulého reportu">
          <GoalBars goals={p.goals} />
        </Panel>
        <Panel title="Agenti: náklady a úspěšnost" right={`$${p.agents.cost_usd.toFixed(2)} · ${p.agents.success_rate === null ? "—" : Math.round(p.agents.success_rate * 100) + " %"}`}>
          <AgentCost p={p} />
        </Panel>
        <Panel title="Vývoj, komunikace, incidenty">
          <Signals p={p} />
        </Panel>
        <Panel fig="8" title="Byznys: peníze, zákazníci, čas majitele" right={p.business ? `byznys $${p.business.cost_split.business_usd.toFixed(2)}` : undefined}>
          <Business p={p} />
        </Panel>
        <Panel title="Největší hotové věci" right={`${p.tasks.highlights.length}`}>
          <TaskList items={p.tasks.highlights} empty="Nic hotového." extra={(i) => {
            const h = p.tasks.highlights[i];
            return h.project ? <span className="cap truncate">{h.project}</span> : null;
          }} />
        </Panel>
        <Panel title="Co stojí" right={`čeká ${p.tasks.waiting} · po termínu ${p.tasks.overdue}`}>
          <TaskList items={[...p.tasks.overdue_list, ...p.tasks.waiting_list]} empty="Nic nestojí." extra={(i) => {
            const all = [...p.tasks.overdue_list.map((t) => `po termínu ${t.deadline ?? ""}`), ...p.tasks.waiting_list.map((t) => `čeká od ${t.since}`)];
            return <span className="cap shrink-0">{all[i]}</span>;
          }} />
        </Panel>
        {p.last_meeting && (
          <Panel title={`Úkoly z minulého meetingu (${p.last_meeting.week})`} right={`${p.last_meeting.tasks_done}/${p.last_meeting.tasks.length} hotovo`} className="lg:col-span-2">
            <TaskList items={p.last_meeting.tasks} empty="Minulý meeting nezadal úkoly." extra={(i) => <span className="cap">{p.last_meeting!.tasks[i].status}</span>} />
          </Panel>
        )}
        <Meeting r={r} />
      </div>
      <p className="cap">
        Čísla spočítal kód {p.generated_at.slice(0, 16).replace("T", " ")} UTC · srovnání: stejná část minulého týdne (tok), poslední report (stav)
        {p.period.partial ? " · týden ještě běží" : ""}
      </p>
    </div>
  );
}

/** Weekly reports by the Chief of Staff (Asistent vedení), browsable by week and printable. */
export default function Reports() {
  const { week } = useParams();
  const navigate = useNavigate();
  const [list, setList] = useState<ReportListItem[] | null>(null);
  const [current, setCurrent] = useState<string>("");
  const [schedule, setSchedule] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadList = useCallback(() =>
    reportsApi.list().then((l) => {
      setList(l.reports);
      setCurrent(l.current_week);
      setSchedule(l.schedule);
      return l;
    }), []);

  useEffect(() => {
    loadList().then((l) => {
      if (!week && l.reports.length) navigate(`/reports/${l.reports[0].week}`, { replace: true });
    }, (e) => setError(e.message));
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
    <div className="flex flex-col gap-5">
      <PageHeader
        kicker="REPORTS · TÝDENNÍ REPORT FIRMY"
        title={week ? `Týden ${week}` : "Reporty"}
        sub={
          report ? (
            <>
              {period} · <span className={STATUS[report.status][1]}>{STATUS[report.status][0]}</span>
            </>
          ) : schedule ? `Asistent vedení píše report a zve na meeting: ${schedule} (Europe/Prague).` : "Týdenní reporty Asistenta vedení."
        }
      />
      <div className="no-print flex flex-wrap items-center gap-2">
        <button className="btn" disabled={!older} onClick={() => older && navigate(`/reports/${older}`)} aria-label="Starší týden">
          <ChevronLeft size={14} />
        </button>
        <select
          className="h-8 rounded border border-line bg-bg px-2 text-[13px]"
          value={week ?? ""}
          onChange={(e) => navigate(`/reports/${e.target.value}`)}
          aria-label="Týden"
        >
          {!week && <option value="">vyber týden</option>}
          {week && !weeks.includes(week) && <option value={week}>{week}</option>}
          {(list ?? []).map((r) => (
            <option key={r.week} value={r.week}>
              {r.week} · {STATUS[r.status][0]}
            </option>
          ))}
        </select>
        <button className="btn" disabled={!newer} onClick={() => newer && navigate(`/reports/${newer}`)} aria-label="Novější týden">
          <ChevronRight size={14} />
        </button>
        {current && !weeks.includes(current) && (
          <button className="btn" disabled={busy} onClick={() => build(current)} title="Spočítat čísla aktuálního týdne (bez agenta, bez tokenů)">
            <RefreshCw size={14} /> Náhled {current}
          </button>
        )}
        {report?.status === "draft" && (
          <button className="btn" disabled={busy} onClick={() => build(report.week)} title="Přepočítat čísla konceptu">
            <RefreshCw size={14} /> Přepočítat
          </button>
        )}
        {report && (
          <button className="btn ml-auto" onClick={() => window.print()}>
            <Printer size={14} /> Tisk / PDF
          </button>
        )}
      </div>
      {error && <p className="cap text-red-400!">{error}</p>}
      {list && !list.length && !week && (
        <Panel title="Zatím žádný report">
          <p className="p-4 text-sm text-ink-2">
            První report vznikne {schedule ? `v termínu „${schedule}“` : "v pátek ve 14:00"}. Čísla aktuálního týdne si můžeš spočítat hned tlačítkem Náhled.
          </p>
        </Panel>
      )}
      {week && !report && !error && list && !weeks.includes(week) && (
        <p className="cap">Pro {week} report není.</p>
      )}
      {report && <ReportView r={report} />}
    </div>
  );
}
