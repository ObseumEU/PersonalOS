import { AlertTriangle, ArrowDownRight, ArrowUpRight, ChevronLeft, Minus, RefreshCw } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { PageHeader, Panel } from "../components/ui";
import company from "../i18n/cs/company";
import { register, t } from "../i18n/core";
import { type Problem, type ScoreGoal, type ScoreKpi, type Scorecard, scorecardApi } from "../scorecardApi";
import { taskHref } from "../taskSheet";

// The installed app (/m) registers only its first screens' strings: this page brings its own.
register(company);

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)} %`);
const usd = (v: number | null | undefined) => (v == null ? "—" : `$${v.toFixed(v >= 100 ? 0 : 2)}`);
const num = (v: number | null | undefined) => (v == null ? "—" : Number.isInteger(v) ? String(v) : v.toFixed(1));

/** One headline number with last week's value; green when the change is good, red when bad. */
function Tile({ label, kpi, fmt = num, href }: { label: string; kpi?: ScoreKpi; fmt?: (v: number) => string; href: string }) {
  const v = kpi?.value ?? null;
  const d = kpi?.delta ?? null;
  const tone = kpi?.good == null ? "var(--color-ink-2)" : kpi.good ? "var(--viz-good)" : "var(--viz-bad)";
  const Icon = !d ? Minus : d > 0 ? ArrowUpRight : ArrowDownRight;
  return (
    <Link to={href} className="panel flex min-w-0 flex-col gap-1.5 px-4 py-3 hover:border-accent/60">
      <span className="truncate text-xs text-ink-2">{label}</span>
      <span className="font-mono text-[26px] leading-none font-light">{v == null ? "—" : fmt(v)}</span>
      <span className="flex flex-wrap items-center gap-1 text-xs" style={{ color: tone }}>
        <Icon size={13} aria-hidden />
        {d == null ? (
          <span className="text-ink-2">{t("co.no_compare")}</span>
        ) : d === 0 ? (
          t("co.flat")
        ) : (
          `${d > 0 ? "+" : "−"}${fmt(Math.abs(d))}`
        )}
        {d != null && kpi?.prev != null && <span className="ml-1 text-ink-2">{t("co.vs", { prev: fmt(kpi.prev) })}</span>}
      </span>
    </Link>
  );
}

function Problems({ items, mobile }: { items: Problem[]; mobile: boolean }) {
  if (!items.length)
    return (
      <p className="panel px-4 py-3 text-sm text-ink-2" role="status">
        {t("co.no_problems")}
      </p>
    );
  return (
    <section aria-label={t("co.problems")} className="grid grid-cols-1 gap-3 md:grid-cols-3">
      {items.map((p, i) => (
        <Link
          key={p.text}
          to={mobile && p.link.startsWith("/company") ? p.link.replace("/company", "/m/company") : p.link}
          className="panel flex min-w-0 gap-3 px-4 py-3 hover:border-accent/60"
          style={{ borderColor: "color-mix(in srgb, var(--viz-warn) 55%, transparent)" }}
        >
          <span className="font-mono text-lg leading-6" style={{ color: "var(--viz-warn)" }}>
            {i + 1}
          </span>
          <span className="flex min-w-0 flex-col gap-1">
            <span className="flex items-center gap-1.5 text-[15px] font-medium">
              <AlertTriangle size={14} style={{ color: "var(--viz-warn)" }} aria-hidden /> {p.text}
            </span>
            <span className="text-xs leading-relaxed text-ink-2">{p.why}</span>
          </span>
        </Link>
      ))}
    </section>
  );
}

/** The goal's `current` over the stored days (a line), or nothing with fewer than two points. */
function Spark({ points, lowerIsBetter }: { points: (number | null)[]; lowerIsBetter: boolean }) {
  const vals = points.filter((p): p is number => p != null);
  if (vals.length < 2) return <span className="inline-block w-[72px]" aria-hidden />;
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const xy = vals.map((v, i) => `${(i / (vals.length - 1)) * 70 + 1},${19 - ((v - min) / span) * 16}`).join(" ");
  const up = vals[vals.length - 1] >= vals[0];
  const color = vals[vals.length - 1] === vals[0] ? "var(--viz-prev)" : up !== lowerIsBetter ? "var(--viz-good)" : "var(--viz-bad)";
  return (
    <svg width="72" height="22" viewBox="0 0 72 22" role="img" aria-label={t("co.goal.numbers", { baseline: vals[0], current: vals[vals.length - 1], target: "" })}>
      <polyline points={xy} fill="none" stroke={color} strokeWidth="1.6" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function GoalRow({ g }: { g: ScoreGoal }) {
  const good = g.delta == null || g.delta === 0 ? null : (g.delta > 0) !== g.lower_is_better;
  return (
    <div className="flex min-w-0 flex-col gap-1.5 border-b border-line px-4 py-3 last:border-0">
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-sm">{g.title}</span>
        {g.status === "proposed" && <span className="rounded border border-line px-1.5 text-[11px] text-ink-2">{t("co.goal.proposed")}</span>}
        <span className="font-mono text-sm">{g.progress} %</span>
      </div>
      <div className="flex items-center gap-3">
        <div className="h-2 flex-1 overflow-hidden rounded-full bg-raised" role="progressbar" aria-valuenow={g.progress} aria-valuemin={0} aria-valuemax={100} aria-label={g.title}>
          <div className="h-full rounded-full" style={{ width: `${Math.max(g.progress, 1)}%`, background: "var(--viz-series-3)" }} />
        </div>
        <Spark points={g.trend.map((p) => p[1])} lowerIsBetter={g.lower_is_better} />
      </div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-xs text-ink-2">
        <span className="font-mono text-ink">{t("co.goal.numbers", { baseline: num(g.baseline), current: num(g.current), target: num(g.target_value) })}</span>
        {g.metric && <span className="truncate">{g.metric}</span>}
        <span>{g.owner ?? t("co.goal.no_owner")}</span>
        {g.due && <span>{t("co.goal.due", { due: g.due })}</span>}
        {good != null && (
          <span style={{ color: good ? "var(--viz-good)" : "var(--viz-bad)" }}>
            {t("co.goal.week", { delta: `${(g.delta ?? 0) > 0 ? "+" : ""}${num(g.delta)}` })}
          </span>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, tone, sub }: { label: string; value: ReactNode; tone?: "bad" | "good" | "warn"; sub?: ReactNode }) {
  const color = tone === "bad" ? "var(--viz-bad)" : tone === "good" ? "var(--viz-good)" : tone === "warn" ? "var(--viz-warn)" : undefined;
  return (
    <div className="flex min-w-0 flex-col gap-0.5 px-4 py-2.5">
      <span className="font-mono text-xl leading-tight" style={{ color }}>
        {value}
      </span>
      <span className="truncate text-xs text-ink-2">{label}</span>
      {sub && <span className="truncate text-[11px] text-ink-3">{sub}</span>}
    </div>
  );
}

/** Business vs platform as one bar, with the ≥ 50 % target and the 30 % platform cap marked. */
function SpendBar({ s }: { s: Scorecard["spend"] }) {
  const b = s.business_share ?? 0;
  const p = s.platform_share ?? 0;
  return (
    <div className="flex flex-col gap-2 px-4 pt-3 pb-1">
      <div className="relative h-4 overflow-hidden rounded bg-raised" role="img" aria-label={`${t("co.spend.business")} ${pct(s.business_share)}, ${t("co.spend.platform")} ${pct(s.platform_share)}`}>
        <div className="absolute inset-y-0 left-0" style={{ width: `${b * 100}%`, background: "var(--viz-series-1)" }} />
        <div className="absolute inset-y-0 right-0" style={{ width: `${p * 100}%`, background: "var(--viz-series-2)", opacity: 0.85 }} />
        <div className="absolute inset-y-0 w-0.5 bg-ink" style={{ left: "50%" }} title={t("co.spend.target")} />
        <div className="absolute inset-y-0 w-0.5 border-l border-dashed border-ink" style={{ left: "70%" }} title={t("co.spend.cap")} />
      </div>
      <div className="flex flex-wrap justify-between gap-x-4 text-xs">
        <span style={{ color: "var(--viz-series-1)" }}>
          {t("co.spend.business")} {pct(s.business_share)} · {usd(s.business_usd)}
        </span>
        <span style={{ color: "var(--viz-series-2)" }}>
          {t("co.spend.platform")} {pct(s.platform_share)} · {usd(s.platform_usd)}
        </span>
      </div>
      <div className="flex flex-wrap gap-x-4 text-[11px] text-ink-2">
        <span>│ {t("co.spend.target")}</span>
        <span>┆ {t("co.spend.cap")}</span>
      </div>
    </div>
  );
}

export default function Company() {
  const loc = useLocation();
  const navigate = useNavigate();
  const mobile = loc.pathname.startsWith("/m/");
  const [card, setCard] = useState<Scorecard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    scorecardApi.get().then(setCard, (e) => setError(String(e.message ?? e)));
  }, []);
  const refresh = () => {
    setBusy(true);
    scorecardApi
      .refresh()
      .then(setCard, (e) => setError(String(e.message ?? e)))
      .finally(() => setBusy(false));
  };
  const task = (ref: string) => (mobile ? `/m/tasks?task=${ref}` : taskHref({ pathname: "/tasks", search: "" }, ref));
  const reviews = mobile ? "/m/tasks" : "/tasks?view=review";

  if (!card)
    return (
      <div className="flex flex-col gap-4 p-4">
        {error ? <p className="text-sm text-red-400">{t("co.error", { error })}</p> : <p className="text-sm text-ink-2" aria-busy="true">{t("co.loading")}</p>}
      </div>
    );
  const k = card.kpis;
  const w = card.world;
  const o = card.owner;
  const a = card.agents;
  const ob = w.outbound;

  return (
    <div className={`report flex flex-col gap-5 ${mobile ? "px-3 pt-[calc(env(safe-area-inset-top)+12px)] pb-6" : ""}`}>
      {mobile ? (
        <header className="flex items-center gap-2">
          <button type="button" onClick={() => navigate("/m/more")} className="grid h-10 w-10 place-items-center rounded-lg text-ink-2 active:bg-raised" aria-label={t("m.more.title")}>
            <ChevronLeft size={22} />
          </button>
          <div className="flex min-w-0 flex-1 flex-col">
            <h1 className="text-xl font-medium">{t("co.title")}</h1>
            <span className="text-xs text-ink-2">
              {t("co.kicker", { day: card.day })}
              {card.compared_with ? ` · ${t("co.compared", { day: card.compared_with })}` : ""}
            </span>
          </div>
          <button type="button" onClick={refresh} disabled={busy} className="grid h-10 w-10 place-items-center rounded-lg text-ink-2 active:bg-raised" aria-label={t("co.refresh")}>
            <RefreshCw size={18} className={busy ? "animate-spin" : ""} />
          </button>
        </header>
      ) : (
        <div className="relative">
          <PageHeader
            kicker={t("co.kicker", { day: card.day }) + (card.compared_with ? ` · ${t("co.compared", { day: card.compared_with })}` : "")}
            title={t("co.title")}
            sub={t("co.sub")}
          />
          <button type="button" onClick={refresh} disabled={busy} className="btn absolute top-0 right-0 h-8! sm:right-[260px]">
            <RefreshCw size={13} className={busy ? "animate-spin" : ""} /> {t("co.refresh")}
          </button>
        </div>
      )}

      <div className="flex flex-col gap-2">
        <h2 className="text-xs font-medium text-ink-2">{t("co.problems")}</h2>
        <Problems items={card.problems} mobile={mobile} />
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <Tile label={t("co.kpi.outbound_sent")} kpi={k.outbound_sent} href="#world" />
        <Tile label={t("co.kpi.owner_delivered_pct")} kpi={k.owner_delivered_pct} fmt={(v) => `${Math.round(v)} %`} href="#owner" />
        <Tile label={t("co.kpi.business_share")} kpi={k.business_share} fmt={(v) => `${Math.round(v * 100)} %`} href="#spend" />
        <Tile label={t("co.kpi.usd_per_delivered")} kpi={k.usd_per_delivered} fmt={(v) => `$${v.toFixed(2)}`} href="#spend" />
        <Tile label={t("co.kpi.review_queue")} kpi={k.review_queue} href={reviews} />
        <Tile label={t("co.kpi.frustrations")} kpi={k.frustrations} href="#agents" />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
        <Panel
          title={t("co.goals")}
          right={mobile ? undefined : <Link to="/reports" className="hover:text-accent">{t("co.goals_link")}</Link>}
          className="min-w-0 lg:col-span-7"
        >
          {card.goals.length ? card.goals.map((g) => <GoalRow key={g.id} g={g} />) : <p className="px-4 py-5 text-sm text-ink-2">{t("co.no_goals")}</p>}
        </Panel>

        <div className="flex min-w-0 flex-col gap-4 lg:col-span-5">
          <section id="world" className="scroll-mt-4">
            <Panel title={t("co.world")}>
              <div className="grid grid-cols-2 sm:grid-cols-3">
                <Stat label={t("co.world.sent")} value={num(ob.sent)} tone={ob.sent === 0 ? "bad" : undefined} />
                <Stat label={t("co.world.replies")} value={num(ob.replies)} />
                <Stat label={t("co.world.failed")} value={num(ob.failed ?? 0)} tone={ob.failed ? "warn" : undefined} sub={ob.not_configured ? `${t("co.world.not_configured")} ${ob.not_configured}` : undefined} />
                <Stat label={t("co.world.published")} value={w.published} />
                <Stat label={t("co.world.deploys")} value={w.deploys_ok} sub={w.deploys_failed ? `${t("co.world.deploys_failed")} ${w.deploys_failed}` : undefined} />
                <Stat label={t("co.world.helped")} value={w.customers_helped} />
              </div>
            </Panel>
          </section>

          <section id="spend" className="scroll-mt-4">
            <Panel title={t("co.spend")} right={`${t("co.spend.total")} ${usd(card.spend.total_usd)}`}>
              <SpendBar s={card.spend} />
              <div className="grid grid-cols-2">
                <Stat label={t("co.spend.per_delivered")} value={usd(card.spend.usd_per_delivered)} sub={t("co.spend.delivered", { n: card.spend.delivered })} />
                <Stat label={t("co.spend.per_business")} value={usd(card.spend.usd_per_business_outcome)} sub={t("co.spend.delivered", { n: card.spend.business_outcomes })} />
              </div>
            </Panel>
          </section>
        </div>

        <section id="owner" className="min-w-0 scroll-mt-4 lg:col-span-6">
          <Panel title={t("co.owner", { days: o.days })}>
            <div className="grid grid-cols-3">
              <Stat label={t("co.owner.delivered")} value={o.delivered_pct == null ? "—" : `${o.delivered_pct} %`} tone={o.delivered_pct != null && o.delivered_pct < 70 ? "bad" : undefined} sub={t("co.owner.of", { done: o.done, total: o.total })} />
              <Stat label={t("co.owner.median")} value={o.median_hours == null ? "—" : t("co.hours", { n: num(o.median_hours) })} />
              <Stat label={t("co.owner.open")} value={o.open} />
            </div>
            {o.oldest_open.length ? (
              <ul className="border-t border-line">
                {o.oldest_open.map((x) => (
                  <li key={x.ref} className="border-b border-line last:border-0">
                    <Link to={task(x.ref)} className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-3 px-4 py-2 hover:bg-raised">
                      <span className="truncate text-sm">{x.title}</span>
                      <span className="text-xs whitespace-nowrap text-ink-2">
                        {x.assignee ?? "—"} · {t("co.owner.age", { n: num(x.age_days) })}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="border-t border-line px-4 py-3 text-sm text-ink-2">{t("co.owner.none_open")}</p>
            )}
          </Panel>
        </section>

        <section id="agents" className="min-w-0 scroll-mt-4 lg:col-span-6">
          <Panel title={t("co.agents")}>
            <div className="grid grid-cols-2 sm:grid-cols-3">
              <Stat label={t("co.agents.runs_ok")} value={a.runs_ok} />
              <Stat label={t("co.agents.runs_failed")} value={a.runs_failed} tone={(a.fail_rate ?? 0) > 0.1 ? "bad" : undefined} sub={`${pct(a.fail_rate)} · ${t("co.agents.runs_blocked")} ${a.runs_blocked}`} />
              <Link to={reviews} className="hover:bg-raised">
                <Stat label={t("co.agents.review")} value={a.review_queue} tone={a.review_queue > 20 ? "bad" : undefined} sub={t("co.agents.review_old", { h: a.review_oldest_hours ?? "—", n: a.review_over_sla })} />
              </Link>
              <Stat label={t("co.agents.loops")} value={a.loops} tone={a.loops ? "warn" : undefined} />
              <Stat label={t("co.agents.incidents")} value={a.incidents} />
              <Stat label={t("co.agents.frustrations")} value={a.frustrations} tone={a.frustrations ? "bad" : undefined} sub={`${t("co.agents.double")} ${a.double_answers} · ${t("co.agents.unanswered")} ${a.unanswered}`} />
            </div>
          </Panel>
        </section>
      </div>
    </div>
  );
}
