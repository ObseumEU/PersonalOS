import {
  ArrowLeft,
  Check,
  CircleDot,
  ExternalLink,
  FileText,
  FolderOpen,
  GitBranch,
  GitCommitHorizontal,
  GitPullRequest,
  Globe,
  Mail,
  MessageSquare,
  Milestone,
  Paperclip,
  Pencil,
  Plus,
  Rocket,
  Scale,
  Send,
  Sparkles,
  Tag,
  Upload,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { confirmDialog, toast } from "../components/overlay";
import Markdown from "../components/Markdown";
import { Avatar, StatusChip, toneOf } from "../components/tasks/bits";
import { oneLine, shortAge } from "../components/tasks/text";
import { PageHeader } from "../components/ui";
import { filesApi } from "../filesApi";
import { LOCALE, t } from "../i18n";
import {
  type ActivityItem,
  type AskAnswer,
  type Capability,
  type KbDoc,
  type Project,
  type ProjectFiles,
  type ProjectStatus,
  type Reality,
  type Summary,
  missing,
  projectsApi,
} from "../projectsApi";
import { setSheetOrder, TaskLink, useSheetOrder } from "../taskSheet";
import { type Actor, type Task, tasksApi } from "../tasksApi";

/* ------------------------------------------------------------------ small pieces */

const STATUS_TONE: Record<ProjectStatus, string> = {
  active: "bg-cyan-400/10 text-cyan-200 ring-cyan-400/40",
  paused: "bg-zinc-400/10 text-zinc-300 ring-zinc-400/25",
  done: "bg-emerald-400/10 text-emerald-200 ring-emerald-400/30",
  archived: "bg-zinc-400/10 text-zinc-400 ring-zinc-400/20",
};
const STATUS_DOT: Record<ProjectStatus, string> = { active: "bg-cyan-300", paused: "bg-zinc-400", done: "bg-emerald-300", archived: "bg-zinc-500" };

function ProjectChip({ status, size = "sm" }: { status: ProjectStatus; size?: "sm" | "md" }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full whitespace-nowrap ring-1 ring-inset ${STATUS_TONE[status]} ${
        size === "md" ? "h-7 px-3 text-[13px] font-medium" : "h-[22px] px-2 text-xs"
      }`}
    >
      <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT[status]}`} />
      {t(`pj.status.${status}`)}
    </span>
  );
}

const kindOf = (k: string | null) => (k === "human" ? "human" : k === "ai" ? "ai" : "agent") as "human" | "ai" | "agent";
const who = (name: string | null) => (name === "Owner" ? t("who.me") : (name ?? "—"));
const day = (d: string | null | undefined, long = false) =>
  d ? new Date(d.length === 10 ? `${d}T12:00:00` : d).toLocaleDateString(LOCALE, long ? { day: "numeric", month: "long", year: "numeric" } : { day: "numeric", month: "numeric", year: "numeric" }) : "";
const csv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const repoName = (r: string) => r.split("/").pop() ?? r;

function Progress({ p, wide = false }: { p: Project; wide?: boolean }) {
  const pct = p.total ? Math.round((p.counts.done / p.total) * 100) : 0;
  return (
    <div className={`flex min-w-0 flex-col gap-1 ${wide ? "w-full sm:w-72" : ""}`}>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-line" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={t("pj.progress")}>
        <div className="h-full rounded-full bg-accent/80" style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-ink-2">
        {p.total ? t("pj.progress_tasks", { done: p.counts.done, total: p.total }) : t("pj.progress_none")}
        {p.info.goal_progress != null && ` · ${t("pj.progress_goal", { n: p.info.goal_progress })}`}
      </span>
    </div>
  );
}

function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex min-w-0 items-center gap-2 pb-2">
      <h3 className="text-xs font-medium tracking-wide text-ink-2 uppercase">{children}</h3>
      {right && <span className="ml-auto flex items-center gap-2">{right}</span>}
    </div>
  );
}

const inp = "h-9 min-w-0 w-full rounded-md border border-line bg-bg px-2.5 text-[13px] outline-none focus:border-accent";
const area = "min-w-0 w-full rounded-md border border-line bg-bg p-2.5 text-[13px] outline-none focus:border-accent";

/* ------------------------------------------------------------------ the list */

type Filter = "active" | "all" | "done";

function ProjectList() {
  const [list, setList] = useState<Project[] | null>(null);
  const [filter, setFilter] = useState<Filter>("active");
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  useEffect(() => {
    projectsApi.list(filter === "done" ? "done" : undefined).then(setList, (e) => setError(e.message));
  }, [filter]);
  const shown = (list ?? []).filter((p) => filter !== "active" || p.status === "active" || p.status === "paused");
  const create = (e: FormEvent) => {
    e.preventDefault();
    projectsApi.create({ name, goal }).then((p) => navigate(`/projects/${p.slug}`), (e2) => setError(e2.message));
  };
  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={t("pj.kicker")} title={t("nav.projects")} sub={t("pj.sub")} />
      <div className="flex flex-wrap items-center gap-2">
        <div role="radiogroup" aria-label={t("tk.list.filter")} className="flex rounded-md border border-line p-0.5">
          {(["active", "all", "done"] as Filter[]).map((f) => (
            <button
              key={f}
              type="button"
              role="radio"
              aria-checked={filter === f}
              onClick={() => setFilter(f)}
              className={`h-8 rounded px-3 text-[13px] ${filter === f ? "bg-raised text-ink" : "text-ink-2 hover:text-ink"}`}
            >
              {t(`pj.filter.${f}`)}
            </button>
          ))}
        </div>
        <form onSubmit={create} className="flex min-w-0 flex-1 flex-wrap justify-end gap-2">
          <input className={`${inp} sm:w-56! w-full`} placeholder={t("pj.new")} value={name} onChange={(e) => setName(e.target.value)} aria-label={t("pj.new_name")} />
          <input className={`${inp} sm:w-72! w-full`} placeholder={t("pj.new_goal")} value={goal} onChange={(e) => setGoal(e.target.value)} aria-label={t("pj.new_goal")} />
          <button type="submit" className="btn-accent h-9!" disabled={!name.trim()}>
            <Plus size={14} /> {t("pj.create")}
          </button>
        </form>
      </div>
      {error && <p className="text-xs break-words text-red-400">{error}</p>}
      {list === null && !error && <p className="text-sm text-ink-2">{t("act.loading")}</p>}
      {list !== null && shown.length === 0 && <p className="text-sm text-ink-2">{t("pj.empty")}</p>}
      <ul className="grid grid-cols-1 gap-3.5 md:grid-cols-2 2xl:grid-cols-3">
        {shown.map((p) => (
          <li key={p.id} className="min-w-0">
            <Link
              to={`/projects/${p.slug}`}
              className="flex h-full min-w-0 flex-col gap-3 rounded-lg border border-line bg-surface p-4 outline-none hover:border-accent/60 focus-visible:border-accent"
            >
              <span className="flex min-w-0 items-start gap-2">
                <span className="min-w-0 flex-1 text-[15px] leading-snug font-medium break-words">{p.name}</span>
                <ProjectChip status={p.status} />
              </span>
              <span className="line-clamp-3 min-h-[3.75em] text-[13px] leading-relaxed break-words text-ink-2">
                {p.summary || p.goal || t("pj.card.no_summary")}
              </span>
              <Progress p={p} />
              <span className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-2">
                <span className="flex min-w-0 items-center gap-1.5">
                  <Avatar type={kindOf(p.lead_kind)} name={p.lead_name} size={20} />
                  <span className="truncate">{t("pj.card.lead", { name: who(p.lead_name) })}</span>
                </span>
                {p.due && <span>{t("pj.card.target", { d: day(p.due) })}</span>}
                {p.info.links.customer && !missing(p.info.links.customer) && <span className="truncate">{p.info.links.customer}</span>}
                {p.info.links.repos.length > 0 && (
                  <span className="flex items-center gap-1">
                    <GitBranch size={12} aria-hidden /> {p.info.links.repos.length}
                  </span>
                )}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/* ------------------------------------------------------------------ header */

function QuickLinks({ p }: { p: Project }) {
  const l = p.info.links;
  const items: { href: string; icon: ReactNode; text: string; internal?: boolean }[] = [
    ...l.repos.map((r) => ({ href: `https://github.com/${r}`, icon: <GitBranch size={13} />, text: repoName(r) })),
    ...(l.drive_folder ? [{ href: l.drive_folder, icon: <FolderOpen size={13} />, text: t("pj.link.drive") }] : []),
    ...(l.website ? [{ href: l.website, icon: <Globe size={13} />, text: l.website.replace(/^https?:\/\//, "").replace(/\/$/, "") }] : []),
    ...(l.customer && !missing(l.customer)
      ? [{ href: l.customer_url ?? "", icon: <Scale size={13} />, text: l.customer }]
      : []),
    ...(p.channel_id ? [{ href: `/chat?c=${p.channel_id}`, icon: <MessageSquare size={13} />, text: t("pj.link.chat", { slug: p.slug }), internal: true }] : []),
  ];
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5" aria-label={t("pj.links")}>
      {items.map((i) =>
        i.internal ? (
          <Link key={i.href} to={i.href} className="inline-flex h-7 items-center gap-1.5 rounded-full border border-line px-2.5 text-xs text-ink-2 hover:border-accent hover:text-ink">
            {i.icon} {i.text}
          </Link>
        ) : i.href ? (
          <a key={i.href + i.text} href={i.href} target="_blank" rel="noreferrer" className="inline-flex h-7 max-w-full items-center gap-1.5 rounded-full border border-line px-2.5 text-xs text-ink-2 hover:border-accent hover:text-ink">
            {i.icon} <span className="truncate">{i.text}</span>
          </a>
        ) : (
          <span key={i.text} className="inline-flex h-7 items-center gap-1.5 rounded-full border border-line px-2.5 text-xs text-ink-2">
            {i.icon} {i.text}
          </span>
        ),
      )}
    </div>
  );
}

function SummaryBox({ summary, loading, onRefresh, mine = [] }: { summary: Summary | null; loading: boolean; onRefresh: () => void; mine?: Task[] }) {
  return (
    <section aria-labelledby="pj-summary" className="rounded-lg border border-line bg-raised/60 px-4 py-3.5">
      <h3 id="pj-summary" className="flex items-center gap-2 pb-1.5 text-xs font-medium tracking-wide text-ink-2 uppercase">
        <Sparkles size={12} aria-hidden /> {t("pj.summary")}
        {loading && <span className="breathe text-xs font-normal tracking-normal normal-case">{t("pj.summary_updating")}</span>}
        {!loading && summary?.source === "fallback" && <span className="text-xs font-normal tracking-normal normal-case">· {t("pj.summary_fallback")}</span>}
        {!loading && (
          <button type="button" onClick={onRefresh} className="ml-auto text-xs font-normal tracking-normal text-ink-2 normal-case hover:text-accent">
            {t("pj.summary_refresh")}
          </button>
        )}
      </h3>
      {summary?.text ? (
        <p className="text-[15px] leading-relaxed text-ink">{summary.text}</p>
      ) : (
        <div className="flex flex-col gap-2" aria-hidden>
          <span className="h-3 w-11/12 rounded bg-line" />
          <span className="h-3 w-3/4 rounded bg-line" />
        </div>
      )}
      {/* "čeká na tvůj zásah": the items themselves, one click away */}
      {mine.length > 0 && (
        <div className="mt-2.5 flex flex-col gap-1 border-t border-line pt-2">
          <span className="text-xs text-amber-200">{t("pj.waits_for_you")}</span>
          {mine.slice(0, 5).map((x) => (
            <TaskLink key={x.ref} taskRef={x.ref} className="flex min-w-0 items-baseline gap-2 text-[14px] hover:text-accent">
              <span className="font-mono text-xs text-ink-2">{x.ref}</span>
              <span className="min-w-0 truncate">{x.title}</span>
            </TaskLink>
          ))}
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ "Co je živé" (pos.reality) */

const REALITY_TONE: Record<Capability["status"], string> = {
  live: "bg-emerald-400/10 text-emerald-200 ring-emerald-400/30",
  unverified: "bg-amber-400/10 text-amber-200 ring-amber-400/30",
  test_only: "bg-zinc-400/10 text-zinc-300 ring-zinc-400/25",
  mock: "bg-zinc-400/10 text-zinc-300 ring-zinc-400/25",
  missing: "bg-red-400/10 text-red-200 ring-red-400/30",
};
const REALITY_LABEL: Record<Capability["status"], () => string> = {
  live: () => t("pj.reality.live"),
  unverified: () => t("pj.reality.unverified"),
  test_only: () => t("pj.reality.test_only"),
  mock: () => t("pj.reality.mock"),
  missing: () => t("pj.reality.missing"),
};
const ACCESS_LABEL: Record<Capability["access"], () => string> = {
  public: () => t("pj.reality.public"),
  password: () => t("pj.reality.password"),
  internal: () => t("pj.reality.internal"),
};

function RealityBox({ slug }: { slug: string }) {
  const [data, setData] = useState<Reality | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(() => {
    projectsApi.reality(slug).then(setData).catch(() => setData(null));
  }, [slug]);
  useEffect(load, [load]);
  if (!data || data.capabilities.length === 0) return null;
  const probe = () => {
    setBusy(true);
    projectsApi
      .probe(slug)
      .then(load)
      .finally(() => setBusy(false));
  };
  const override = async (id: number) => {
    const reason = await confirmDialog({ title: t("pj.reality_override"), confirm: t("pj.reality_override"), reason: t("pj.reality_override_reason") });
    if (reason === null) return;
    await projectsApi.override(id, reason);
    toast(t("pj.reality_override_done"));
    load();
  };
  const release = async (taskId: number) => {
    await projectsApi.release(slug, taskId);
    toast(t("pj.reality_released"));
    load();
  };
  return (
    <section aria-labelledby="pj-reality" className="rounded-lg border border-line px-4 py-3.5">
      <h3 id="pj-reality" className="flex items-center gap-2 pb-1 text-xs font-medium tracking-wide text-ink-2 uppercase">
        <Globe size={12} aria-hidden /> {t("pj.reality")}
        <button type="button" onClick={probe} disabled={busy} className="ml-auto text-xs font-normal tracking-normal text-ink-2 normal-case hover:text-accent">
          {busy ? t("pj.reality_checking") : t("pj.reality_check")}
        </button>
      </h3>
      <p className="pb-2 text-xs text-ink-2">{t("pj.reality_hint", { hours: Math.round(data.expiry_hours) })}</p>
      <ul className="flex flex-col divide-y divide-line">
        {data.capabilities.map((c) => (
          <li key={c.key} className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 py-1.5 text-[14px]">
            <span className={`shrink-0 rounded px-1.5 text-xs ring-1 ${REALITY_TONE[c.status]}`}>{REALITY_LABEL[c.status]()}</span>
            <span className="min-w-0 flex-1">{c.name}</span>
            <span className="text-xs text-ink-2">
              {ACCESS_LABEL[c.access]()}
              {" · "}
              {c.expired
                ? t("pj.reality_expired")
                : c.status === "test_only" && c.verified && c.verified_at
                  ? t("pj.reality_verified_test", { age: shortAge(c.verified_at) })
                  : c.status === "test_only" && c.verified_at
                    ? t("pj.reality_expired")
                    : c.verified_at
                      ? t("pj.reality_verified", { age: shortAge(c.verified_at) })
                      : t("pj.reality_never")}
            </span>
            {c.url && (
              <a href={c.url} target="_blank" rel="noreferrer" className="basis-full truncate text-xs text-ink-2 hover:text-accent">
                {c.url}
              </a>
            )}
            {c.last_check_detail && c.last_check_ok === false && <span className="basis-full text-xs text-amber-200">{c.last_check_detail}</span>}
          </li>
        ))}
      </ul>
      {(data.holds?.length ?? 0) > 0 && (
        <div className="mt-2.5 flex flex-col gap-1.5 border-t border-line pt-2">
          <span className="text-xs text-amber-200">{t("pj.reality_holds")}</span>
          <span className="text-xs text-ink-2">{t("pj.reality_holds_hint")}</span>
          {data.holds!.map((h) => (
            <div key={h.task_id} className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-[13px]">
              <span className="min-w-0 flex-1">
                <span className="text-ink-2">{h.ref} · {h.assignee ?? "?"} · </span>
                {h.title}
              </span>
              {data.can_release && (
                <button type="button" onClick={() => release(h.task_id)} className="text-xs text-ink-2 hover:text-accent">
                  {t("pj.reality_release")}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {data.blocked.length > 0 && (
        <div className="mt-2.5 flex flex-col gap-1.5 border-t border-line pt-2">
          <span className="text-xs text-amber-200">{t("pj.reality_blocked")}</span>
          <span className="text-xs text-ink-2">{t("pj.reality_blocked_hint")}</span>
          {data.blocked.slice(0, 5).map((b) => (
            <div key={b.id} className="flex min-w-0 flex-col gap-0.5 text-[13px]">
              <span className="min-w-0">
                <span className="text-ink-2">{b.agent ?? "?"} · {shortAge(b.at)} · </span>
                {b.reasons.join("; ")}
              </span>
              {data.can_override &&
                (b.overridden ? (
                  <span className="text-xs text-ink-2">{t("pj.reality_overridden")}</span>
                ) : (
                  <button type="button" onClick={() => override(b.id)} className="self-start text-xs text-ink-2 hover:text-accent">
                    {t("pj.reality_override")}
                  </button>
                ))}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------ side column: details and editing */

function MetaRow({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 py-2">
      <dt className="text-xs text-ink-2">{k}</dt>
      <dd className="min-w-0 text-[13px] break-words text-ink">{children}</dd>
    </div>
  );
}

const Todo = () => <span className="text-ink-3 italic">{t("pj.todo")}</span>;
const val = (v: string | null | undefined) => (missing(v) ? <Todo /> : v);

function Meta({ p, onEdit }: { p: Project; onEdit: () => void }) {
  const f = p.info.facts;
  return (
    <div className="flex flex-col">
      <SectionTitle
        right={
          p.can_edit && (
            <button type="button" onClick={onEdit} className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
              <Pencil size={12} /> {t("pj.edit")}
            </button>
          )
        }
      >
        {t("pj.facts")}
      </SectionTitle>
      <dl className="flex flex-col divide-y divide-line">
        {/* Only what is filled in: no "doplnit" / "neurčeno" placeholders for the owner to read. */}
        {!missing(f.customer ?? p.info.links.customer) && <MetaRow k={t("pj.fact.customer")}>{val(f.customer ?? p.info.links.customer)}</MetaRow>}
        {!missing(f.contact) && <MetaRow k={t("pj.fact.contact")}>{val(f.contact)}</MetaRow>}
        {!missing(f.budget) && <MetaRow k={t("pj.fact.budget")}>{val(f.budget)}</MetaRow>}
        {!missing(f.stack) && <MetaRow k={t("pj.fact.stack")}>{val(f.stack)}</MetaRow>}
        {p.info.start_date && <MetaRow k={t("pj.start")}>{day(p.info.start_date, true)}</MetaRow>}
        {p.due && <MetaRow k={t("pj.target")}>{day(p.due, true)}</MetaRow>}
        {p.info.kb_workspace && <MetaRow k={t("pj.kb")}>#{p.info.kb_workspace}</MetaRow>}
        {(p.labels.length > 0 || p.info.keywords.length > 0) && (
          <MetaRow k={t("pj.labels")}>
            <span className="flex flex-wrap gap-1">
              {[...p.labels.map((l) => `#${l}`), ...p.info.keywords].map((l) => (
                <span key={l} className="inline-flex items-center gap-1 rounded bg-raised px-1.5 text-xs text-ink-2">
                  <Tag size={10} aria-hidden /> {l}
                </span>
              ))}
            </span>
          </MetaRow>
        )}
      </dl>
    </div>
  );
}

function Field({ k, children }: { k: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-xs text-ink-2">{k}</span>
      {children}
    </label>
  );
}

function MetaEdit({ p, actors, save, onDone }: { p: Project; actors: Actor[]; save: (c: Record<string, unknown>) => void; onDone: () => void }) {
  const k = p.updated_at;
  const blur = (field: string, current: string | null | undefined, wrap?: (v: string) => unknown) => (e: { target: { value: string } }) => {
    const v = e.target.value.trim();
    if (v === (current ?? "")) return;
    save(wrap ? (wrap(v) as Record<string, unknown>) : { [field]: v || null });
  };
  const L = p.info.links;
  const F = p.info.facts;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center">
        <h3 className="text-xs font-medium tracking-wide text-ink-2 uppercase">{t("pj.editing")}</h3>
        <button type="button" onClick={onDone} className="btn-accent ml-auto h-8!">
          <Check size={14} /> {t("pj.done_editing")}
        </button>
      </div>
      <Field k={t("pj.f.name")}>
        <input key={`n${k}`} className={inp} defaultValue={p.name} onBlur={blur("name", p.name)} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field k={t("pj.f.status")}>
          <select className={inp} value={p.status} onChange={(e) => save({ status: e.target.value })}>
            {(["active", "paused", "done", "archived"] as const).map((s) => (
              <option key={s} value={s}>
                {t(`pj.status.${s}`)}
              </option>
            ))}
          </select>
        </Field>
        <Field k={t("pj.f.lead")}>
          <select className={inp} value={p.lead_id ?? ""} onChange={(e) => e.target.value && save({ lead: Number(e.target.value) })}>
            {actors.map((a) => (
              <option key={a.id} value={a.id}>
                {a.is_owner ? t("who.me") : a.name}
              </option>
            ))}
          </select>
        </Field>
        <Field k={t("pj.f.start")}>
          <input type="date" className={inp} value={p.info.start_date ?? ""} onChange={(e) => save({ start_date: e.target.value || null })} />
        </Field>
        <Field k={t("pj.f.target")}>
          <input type="date" className={inp} value={p.due ?? ""} onChange={(e) => save({ due: e.target.value || null })} />
        </Field>
        <Field k={t("pj.f.goal_progress")}>
          <input key={`g${k}`} type="number" min={0} max={100} className={inp} defaultValue={p.info.goal_progress ?? ""} onBlur={blur("goal_progress", p.info.goal_progress?.toString(), (v) => ({ goal_progress: v === "" ? null : Number(v) }))} />
        </Field>
        <Field k={t("pj.f.visibility")}>
          <select className={inp} value={p.visibility} onChange={(e) => save({ visibility: e.target.value })}>
            <option value="team">{t("work.visibility.team")}</option>
            <option value="private">{t("work.visibility.private")}</option>
            <option value="public">{t("work.visibility.public")}</option>
          </select>
        </Field>
      </div>
      <Field k={t("pj.f.repos")}>
        <input key={`r${k}`} className={inp} defaultValue={L.repos.join(", ")} onBlur={blur("repos", L.repos.join(", "), (v) => ({ links: { repos: csv(v) } }))} />
      </Field>
      <Field k={t("pj.f.drive")}>
        <input key={`d${k}`} className={inp} defaultValue={L.drive_folder ?? ""} onBlur={blur("drive", L.drive_folder, (v) => ({ links: { drive_folder: v || null } }))} />
      </Field>
      <Field k={t("pj.f.website")}>
        <input key={`w${k}`} className={inp} defaultValue={L.website ?? ""} onBlur={blur("website", L.website, (v) => ({ links: { website: v || null } }))} />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field k={t("pj.f.customer")}>
          <input key={`c${k}`} className={inp} defaultValue={L.customer ?? ""} onBlur={blur("customer", L.customer, (v) => ({ links: { customer: v || null } }))} />
        </Field>
        <Field k={t("pj.f.customer_url")}>
          <input key={`cu${k}`} className={inp} defaultValue={L.customer_url ?? ""} onBlur={blur("customer_url", L.customer_url, (v) => ({ links: { customer_url: v || null } }))} />
        </Field>
      </div>
      {(["customer", "contact", "budget", "stack"] as const).map((f) => (
        <Field key={f} k={t(`pj.fact.${f}`)}>
          <input key={`f${f}${k}`} className={inp} defaultValue={F[f] ?? ""} onBlur={blur(f, F[f], (v) => ({ facts: { [f]: v || null } }))} />
        </Field>
      ))}
      <Field k={t("pj.f.kb")}>
        <input key={`kb${k}`} className={inp} defaultValue={p.info.kb_workspace ?? ""} onBlur={blur("kb_workspace", p.info.kb_workspace)} />
      </Field>
      <label className="flex items-start gap-2 text-[13px]">
        <input type="checkbox" className="mt-0.5" checked={p.info.kb_strict} onChange={(e) => save({ kb_strict: e.target.checked })} />
        <span>{t("pj.f.kb_strict")}</span>
      </label>
      <Field k={t("pj.f.labels")}>
        <input key={`l${k}`} className={inp} defaultValue={p.labels.join(", ")} onBlur={blur("labels", p.labels.join(", "), (v) => ({ labels: csv(v) }))} />
      </Field>
      <Field k={t("pj.f.keywords")}>
        <input key={`kw${k}`} className={inp} defaultValue={p.info.keywords.join(", ")} onBlur={blur("keywords", p.info.keywords.join(", "), (v) => ({ keywords: csv(v) }))} />
        <span className="text-xs text-ink-3">{t("pj.keywords_hint")}</span>
      </Field>
    </div>
  );
}

/* ------------------------------------------------------------------ Přehled */

function EditableText({
  label,
  value,
  empty,
  canEdit,
  markdown = false,
  rows = 3,
  onSave,
}: {
  label: string;
  value: string | null;
  empty: string;
  canEdit: boolean;
  markdown?: boolean;
  rows?: number;
  onSave: (v: string) => Promise<unknown>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  useEffect(() => setDraft(value ?? ""), [value]);
  const shown = missing(value) ? null : value;
  return (
    <section className="flex min-w-0 flex-col">
      <SectionTitle
        right={
          canEdit &&
          !editing && (
            <button type="button" onClick={() => setEditing(true)} className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
              <Pencil size={12} /> {t("pj.edit")}
            </button>
          )
        }
      >
        {label}
      </SectionTitle>
      {editing ? (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            onSave(draft).then(() => setEditing(false));
          }}
        >
          <textarea className={area} rows={rows} value={draft} onChange={(e) => setDraft(e.target.value)} autoFocus aria-label={label} />
          {markdown && <span className="text-xs text-ink-3">{t("pj.description_hint")}</span>}
          <span className="flex gap-2">
            <button type="submit" className="btn-accent h-8!">
              {t("pj.save")}
            </button>
            <button
              type="button"
              className="btn h-8!"
              onClick={() => {
                setDraft(value ?? "");
                setEditing(false);
              }}
            >
              {t("pj.cancel")}
            </button>
          </span>
        </form>
      ) : shown ? (
        markdown ? (
          <Markdown text={shown} />
        ) : (
          <p className="text-[14px] leading-relaxed break-words whitespace-pre-wrap">{shown}</p>
        )
      ) : (
        canEdit ? (
          <button type="button" className="self-start text-[13px] text-accent hover:underline" onClick={() => setEditing(true)}>
            + {t("pj.fill_in", { what: label.toLowerCase() })}
          </button>
        ) : (
          <p className="text-[13px] text-ink-2">{empty}</p>
        )
      )}
    </section>
  );
}

function Decisions({ p, onChange }: { p: Project; onChange: () => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [why, setWhy] = useState("");
  const [whoName, setWho] = useState("");
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10));
  const list = (p.decisions ?? []).filter((d) => d.kind === "decision");
  const add = (e: FormEvent) => {
    e.preventDefault();
    projectsApi.decide(p.slug, { text, why, who: whoName || undefined, date }).then(
      () => {
        setText("");
        setWhy("");
        setWho("");
        setOpen(false);
        onChange();
      },
      (err) => toast(err.message, { error: true }),
    );
  };
  return (
    <section className="flex min-w-0 flex-col">
      <SectionTitle
        right={
          !open && (
            <button type="button" onClick={() => setOpen(true)} className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
              <Plus size={12} /> {t("pj.decision.add")}
            </button>
          )
        }
      >
        {t("pj.decisions")}
      </SectionTitle>
      {open && (
        <form onSubmit={add} className="mb-3 grid gap-2 rounded-lg border border-line p-3 sm:grid-cols-[1fr_140px]">
          <input className={inp} placeholder={t("pj.decision.text")} aria-label={t("pj.decision.text")} value={text} onChange={(e) => setText(e.target.value)} autoFocus />
          <input type="date" className={inp} aria-label={t("pj.decision.date")} value={date} onChange={(e) => setDate(e.target.value)} />
          <input className={inp} placeholder={t("pj.decision.why")} aria-label={t("pj.decision.why")} value={why} onChange={(e) => setWhy(e.target.value)} />
          <input className={inp} placeholder={t("pj.decision.who")} aria-label={t("pj.decision.who")} value={whoName} onChange={(e) => setWho(e.target.value)} />
          <span className="flex gap-2 sm:col-span-2">
            <button type="submit" className="btn-accent h-8!" disabled={!text.trim()}>
              {t("pj.save")}
            </button>
            <button type="button" className="btn h-8!" onClick={() => setOpen(false)}>
              {t("pj.cancel")}
            </button>
          </span>
        </form>
      )}
      {list.length === 0 && !open && <p className="text-[13px] text-ink-2">{t("pj.decisions_empty")}</p>}
      <ol className="flex flex-col">
        {list.map((d) => (
          <li key={d.id} className="group grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-0.5 border-b border-line py-2.5 last:border-0 sm:grid-cols-[88px_minmax(0,1fr)_auto]">
            <span className="col-span-2 pt-px text-xs text-ink-2 tabular-nums sm:col-span-1">{day(d.date)}</span>
            <span className="flex min-w-0 flex-col gap-0.5">
              <Markdown text={d.text} compact className="text-[14px]" />
              {d.why && <Markdown text={d.why} compact className="text-[13px] text-ink-2" />}
              {d.who && <span className="text-[13px] text-ink-3">{d.who}</span>}
              {d.source?.startsWith("http") && (
                <a href={d.source} target="_blank" rel="noreferrer" className="self-start text-xs text-accent hover:underline">
                  {t("pj.files.open")}
                </a>
              )}
            </span>
            <button
              type="button"
              aria-label={t("pj.decision.remove")}
              title={t("pj.decision.remove")}
              className="h-7 w-7 self-start rounded text-ink-3 opacity-0 group-hover:opacity-100 hover:text-red-300 focus:opacity-100"
              onClick={async () => {
                if ((await confirmDialog({ title: t("pj.decision.remove_ask", { text: d.text.slice(0, 60) }), confirm: t("pj.decision.remove") })) === null) return;
                await projectsApi.dropDecision(p.slug, d.id);
                onChange();
              }}
            >
              <X size={14} className="mx-auto" />
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Overview({ p, save, onChange }: { p: Project; save: (c: Record<string, unknown>) => Promise<unknown>; onChange: () => void }) {
  return (
    <div className="flex flex-col gap-7">
      <EditableText label={t("pj.description")} value={p.info.description} empty={t("pj.description_empty")} canEdit={!!p.can_edit} markdown rows={12} onSave={(v) => save({ description: v })} />
      <div className="grid gap-6 md:grid-cols-2">
        <EditableText label={t("pj.goal")} value={p.goal} empty={t("pj.unset")} canEdit={!!p.can_edit} onSave={(v) => save({ goal: v })} />
        <EditableText label={t("pj.dod")} value={p.definition_of_done} empty={t("pj.unset")} canEdit={!!p.can_edit} onSave={(v) => save({ definition_of_done: v })} />
      </div>
      <Decisions p={p} onChange={onChange} />
    </div>
  );
}

/* ------------------------------------------------------------------ Soubory */

const size = (n: number) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} kB` : `${(n / 1024 / 1024).toFixed(1)} MB`);

function DocRow({ d, icon }: { d: KbDoc; icon: ReactNode }) {
  const body = (
    <>
      <span className="text-ink-2" aria-hidden>
        {icon}
      </span>
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[13px] group-hover:text-accent">{d.kind === "repo" ? d.path : d.name}</span>
        <span className="truncate text-xs text-ink-2">{[d.kind === "repo" ? repoName(d.repo ?? "") : d.channel, d.date ? day(d.date) : null].filter(Boolean).join(" · ")}</span>
      </span>
      {d.url && <ExternalLink size={12} className="shrink-0 text-ink-3" aria-hidden />}
    </>
  );
  return d.url ? (
    <a href={d.url} target="_blank" rel="noreferrer" className="group grid grid-cols-[18px_minmax(0,1fr)_auto] items-center gap-2.5 rounded px-2 py-1.5 -mx-2 hover:bg-raised">
      {body}
    </a>
  ) : (
    <div className="grid grid-cols-[18px_minmax(0,1fr)_auto] items-center gap-2.5 px-2 py-1.5 -mx-2">{body}</div>
  );
}

function DocGroup({ title, docs, empty, icon, right }: { title: string; docs: KbDoc[]; empty: string; icon: ReactNode; right?: ReactNode }) {
  const [all, setAll] = useState(false);
  const shown = all ? docs : docs.slice(0, 8);
  return (
    <section className="flex min-w-0 flex-col">
      <SectionTitle right={right}>
        {title} {docs.length > 0 && <span className="ml-1 rounded-full bg-raised px-1.5 font-normal tabular-nums normal-case">{docs.length}</span>}
      </SectionTitle>
      {docs.length === 0 ? <p className="text-[13px] text-ink-2">{empty}</p> : shown.map((d) => <DocRow key={d.id} d={d} icon={icon} />)}
      {docs.length > shown.length && (
        <button type="button" className="self-start pt-1 text-xs text-accent hover:underline" onClick={() => setAll(true)}>
          {t("tk.list.show_more", { n: docs.length - shown.length })}
        </button>
      )}
    </section>
  );
}

function Files({ p, save }: { p: Project; save: (c: Record<string, unknown>) => Promise<unknown> }) {
  const [data, setData] = useState<ProjectFiles | null>(null);
  const [busy, setBusy] = useState(false);
  const [folder, setFolder] = useState("");
  const [connecting, setConnecting] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const load = useCallback(() => projectsApi.files(p.slug).then(setData, () => setData(null)), [p.slug]);
  useEffect(() => {
    load();
  }, [load, p.info.links.drive_folder]);
  const upload = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    try {
      for (const f of Array.from(files)) {
        const item = await filesApi.upload(f, { topic: p.labels[0] });
        await projectsApi.linkFile(p.slug, item.id);
      }
      toast(t("pj.files.uploaded"));
      load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  };
  const hasKb = !!(p.info.kb_workspace || p.info.links.repos.length || p.info.links.drive_folder);
  return (
    <div className="flex flex-col gap-7">
      <section className="flex min-w-0 flex-col">
        <SectionTitle
          right={
            <>
              <input ref={input} type="file" multiple className="hidden" onChange={(e) => upload(e.target.files)} aria-label={t("pj.files.upload")} />
              <button type="button" className="btn h-8!" disabled={busy} onClick={() => input.current?.click()}>
                <Upload size={14} /> {busy ? t("pj.files.uploading") : t("pj.files.upload")}
              </button>
            </>
          }
        >
          {t("pj.files.local")}
        </SectionTitle>
        {data && data.files.length === 0 && <p className="text-[13px] text-ink-2">{t("pj.files.local_empty")}</p>}
        <ul className="flex flex-col">
          {data?.files.map((f) => (
            <li key={f.id} className="group grid grid-cols-[18px_minmax(0,1fr)_auto] items-center gap-2.5 border-b border-line py-2 last:border-0">
              <Paperclip size={15} className="text-ink-2" aria-hidden />
              <Link to={`/files?file=${f.id}`} className="flex min-w-0 flex-col hover:text-accent">
                <span className="truncate text-[13px]">{f.name}</span>
                <span className="text-xs text-ink-2">
                  {size(f.size)} · {day(f.created_at)}
                </span>
              </Link>
              <span className="flex items-center gap-1">
                <a href={filesApi.contentUrl(f.id)} target="_blank" rel="noreferrer" className="btn h-7! px-2! text-xs" aria-label={`${t("pj.files.open")} ${f.name}`}>
                  <ExternalLink size={12} />
                </a>
                <button
                  type="button"
                  className="btn h-7! px-2! text-xs opacity-60 group-hover:opacity-100"
                  title={t("pj.files.unlink")}
                  aria-label={`${t("pj.files.unlink")}: ${f.name}`}
                  onClick={() => projectsApi.unlinkFile(p.slug, f.id).then(load)}
                >
                  <X size={12} />
                </button>
              </span>
            </li>
          ))}
        </ul>
      </section>
      {data && !data.available && <p className="rounded-md border border-line px-3 py-2 text-[13px] text-ink-2">{t("pj.files.kb_off")}</p>}
      {data && data.available && !hasKb && <p className="rounded-md border border-line px-3 py-2 text-[13px] text-ink-2">{t("pj.files.kb_none")}</p>}
      {data?.available && hasKb && (
        <>
          <DocGroup title={t("pj.files.repo")} docs={data.repo_docs} empty={t("pj.files.repo_empty")} icon={<FileText size={15} />} />
          <DocGroup
            title={t("pj.files.drive")}
            docs={data.drive}
            empty={t("pj.files.drive_empty")}
            icon={<FolderOpen size={15} />}
            right={
              data.drive_folder ? (
                <a href={data.drive_folder} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
                  {t("pj.files.drive_open")} <ExternalLink size={11} />
                </a>
              ) : (
                p.can_edit &&
                !connecting && (
                  <button type="button" className="inline-flex items-center gap-1 text-xs text-accent hover:underline" onClick={() => setConnecting(true)}>
                    <Plus size={12} /> {t("pj.files.drive_connect")}
                  </button>
                )
              )
            }
          />
          {connecting && (
            <form
              className="-mt-4 flex flex-col gap-1.5"
              onSubmit={(e) => {
                e.preventDefault();
                save({ links: { drive_folder: folder.trim() } }).then(() => setConnecting(false));
              }}
            >
              <span className="flex gap-2">
                <input className={inp} placeholder={t("pj.files.drive_placeholder")} value={folder} onChange={(e) => setFolder(e.target.value)} autoFocus aria-label={t("pj.files.drive_connect")} />
                <button type="submit" className="btn-accent h-9!" disabled={!folder.trim()}>
                  {t("pj.save")}
                </button>
              </span>
              <span className="text-xs text-ink-3">{t("pj.files.drive_hint")}</span>
            </form>
          )}
          <DocGroup title={t("pj.files.mail")} docs={data.mail} empty={t("pj.files.mail_empty")} icon={<Mail size={15} />} />
        </>
      )}
      {!data && <p className="text-sm text-ink-2">{t("act.loading")}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ Úkoly */

const TASK_GROUPS: { key: string; statuses: Task["status"][] }[] = [
  { key: "working", statuses: ["working"] },
  { key: "review", statuses: ["review"] },
  { key: "next", statuses: ["next", "inbox"] },
  { key: "waiting", statuses: ["waiting", "someday"] },
  { key: "done", statuses: ["done"] },
];

function TaskRow({ task }: { task: Task }) {
  const line = oneLine(task);
  const done = task.status === "done";
  return (
    <li>
      <TaskLink taskRef={task.ref} className="grid grid-cols-[30px_minmax(0,1fr)_auto] items-center gap-x-3 border-b border-line px-1 py-2.5 outline-none hover:bg-raised/70 focus-visible:bg-raised sm:grid-cols-[30px_minmax(0,1fr)_auto_48px]">
        <Avatar type={task.assignee_type} name={task.assignee_name} size={28} />
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className={`truncate text-[14px] ${done ? "text-ink-2" : "text-ink"}`}>{task.title}</span>
          {line && <span className="truncate text-[13px] text-ink-2">{line}</span>}
        </span>
        <StatusChip tone={toneOf(task)} progress={task.status === "working" ? task.progress : null} />
        <span className="hidden text-right text-xs text-ink-2 tabular-nums sm:block">{shortAge(task.updated_at)}</span>
      </TaskLink>
    </li>
  );
}

function Tasks({ p, onChange }: { p: Project; onChange: () => void }) {
  const [text, setText] = useState("");
  const [showDone, setShowDone] = useState(false);
  const all = p.tasks ?? [];
  const groups = TASK_GROUPS.map((g) => ({ ...g, items: all.filter((x) => g.statuses.includes(x.status)) })).filter((g) => g.items.length);
  const order = useMemo(() => groups.flatMap((g) => g.items.map((x) => x.ref)), [groups]);
  useEffect(() => setSheetOrder(order), [order]);
  useEffect(() => () => setSheetOrder([]), []);
  return (
    <div className="flex flex-col gap-4">
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!text.trim()) return;
          const m = text.match(/@([^@]+)$/);
          api<Task>("/api/tasks", {
            method: "POST",
            body: JSON.stringify({ title: text.replace(/@[^@]+$/, "").trim(), project: p.slug, status: "next", ...(m ? { assignee: m[1].trim() } : {}) }),
          })
            .then(() => {
              setText("");
              onChange();
            }, (err: Error) => toast(err.message, { error: true }));
        }}
      >
        <input className={inp} placeholder={t("pj.tasks.add")} aria-label={t("pj.tasks.add_aria")} value={text} onChange={(e) => setText(e.target.value)} />
        <button type="submit" className="btn-accent h-9!" disabled={!text.trim()} aria-label={t("pj.tasks.add_aria")}>
          <Plus size={14} />
        </button>
      </form>
      {all.length === 0 && <p className="text-[13px] text-ink-2">{t("pj.tasks.empty")}</p>}
      {groups.map((g) => {
        const items = g.key === "done" && !showDone ? g.items.slice(0, 5) : g.items;
        return (
          <section key={g.key} aria-labelledby={`pjg-${g.key}`}>
            <h4 id={`pjg-${g.key}`} className="flex items-center gap-2 pb-1">
              <span className="text-[13px] font-medium">{t(`pj.tasks.group.${g.key}`)}</span>
              <span className="rounded-full bg-raised px-2 text-xs text-ink-2 tabular-nums">{g.items.length}</span>
            </h4>
            <ul>
              {items.map((x) => (
                <TaskRow key={x.id} task={x} />
              ))}
            </ul>
            {items.length < g.items.length && (
              <button type="button" className="pt-2 text-xs text-accent hover:underline" onClick={() => setShowDone(true)}>
                {t("tk.list.show_more", { n: g.items.length - items.length })}
              </button>
            )}
          </section>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ Lidé a agenti */

function People({ p, save, onChange }: { p: Project; save: (c: Record<string, unknown>) => Promise<unknown>; onChange: () => void }) {
  const [member, setMember] = useState("");
  return (
    <div className="flex flex-col gap-4">
      <ul className="grid gap-3 md:grid-cols-2">
        {(p.people ?? []).map((m) => (
          <li key={m.actor_id} className="flex min-w-0 flex-col gap-2.5 rounded-lg border border-line p-3.5">
            <div className="flex min-w-0 items-center gap-2.5">
              <Avatar type={kindOf(m.kind)} name={m.name} size={34} />
              <span className="flex min-w-0 flex-1 flex-col leading-tight">
                <span className="truncate text-[14px]">{who(m.name)}</span>
                <span className="text-xs text-ink-2">
                  {t(`pj.people.${m.role}`)}
                  {m.project_role && ` · ${m.project_role}`}
                </span>
              </span>
              {p.can_edit && m.role === "member" && (
                <button type="button" className="shrink-0 text-xs text-ink-2 hover:text-accent" onClick={() => projectsApi.addMember(p.slug, m.actor_id, "lead").then(onChange)}>
                  {t("pj.people.make_lead")}
                </button>
              )}
            </div>
            {p.can_edit && m.role !== "helper" && (
              <input
                key={`${m.actor_id}${p.updated_at}`}
                className={`${inp} h-8!`}
                placeholder={t("pj.people.role_placeholder")}
                aria-label={`${t("pj.people.role")}: ${m.name}`}
                defaultValue={m.project_role ?? ""}
                onBlur={(e) => e.target.value.trim() !== (m.project_role ?? "") && save({ member_roles: { [m.actor_id]: e.target.value.trim() || null } })}
              />
            )}
            <div className="flex flex-col gap-1">
              <span className="text-xs text-ink-2">{t("pj.people.now")}</span>
              {m.now.length === 0 && <span className="text-[13px] text-ink-3">{t("pj.people.idle")}</span>}
              {m.now.map((x) => (
                <TaskLink key={x.ref} taskRef={x.ref} className="group flex min-w-0 items-center gap-2 rounded px-1 py-0.5 -mx-1 hover:bg-raised">
                  <StatusChip tone={x.status} progress={x.status === "working" ? x.progress : null} />
                  <span className="min-w-0 truncate text-[13px] group-hover:text-accent">{x.title}</span>
                </TaskLink>
              ))}
            </div>
          </li>
        ))}
      </ul>
      {p.can_edit && (
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (member.trim())
              projectsApi.addMember(p.slug, member.trim()).then(
                () => {
                  setMember("");
                  onChange();
                },
                (err) => toast(err.message, { error: true }),
              );
          }}
        >
          <input className={inp} placeholder={t("pj.people.add")} aria-label={t("pj.people.add")} value={member} onChange={(e) => setMember(e.target.value)} />
          <button type="submit" className="btn h-9!" disabled={!member.trim()} aria-label={t("pj.people.add")}>
            <Plus size={14} />
          </button>
        </form>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ Aktivita */

const KIND_ICON: Record<ActivityItem["kind"], ReactNode> = {
  commit: <GitCommitHorizontal size={14} />,
  deploy: <Rocket size={14} />,
  deploy_fail: <Rocket size={14} className="text-red-300" />,
  pr: <GitPullRequest size={14} />,
  issue: <CircleDot size={14} />,
  release: <Milestone size={14} />,
  mail: <Mail size={14} />,
  task_new: <Plus size={14} />,
  task_done: <Check size={14} className="text-emerald-300" />,
  decision: <Scale size={14} className="text-amber-200" />,
  milestone: <Milestone size={14} className="text-accent" />,
};

function dayLabel(iso: string) {
  const d = new Date(iso.slice(0, 10) + "T12:00:00");
  const today = new Date();
  const diff = Math.round((new Date(today.toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 86400000);
  if (diff === 0) return t("pj.activity.today");
  if (diff === 1) return t("pj.activity.yesterday");
  return d.toLocaleDateString(LOCALE, { weekday: "long", day: "numeric", month: "long" });
}

function Activity({ p }: { p: Project }) {
  const [days, setDays] = useState(30);
  const [items, setItems] = useState<ActivityItem[] | null>(null);
  useEffect(() => {
    setItems(null);
    projectsApi.activity(p.slug, days).then((r) => setItems(r.items), () => setItems([]));
  }, [p.slug, days, p.updated_at]);
  const byDay = useMemo(() => {
    const out: [string, ActivityItem[]][] = [];
    for (const i of items ?? []) {
      const d = i.at.slice(0, 10);
      if (out.length && out[out.length - 1][0] === d) out[out.length - 1][1].push(i);
      else out.push([d, [i]]);
    }
    return out;
  }, [items]);
  if (items === null) return <p className="text-sm text-ink-2">{t("act.loading")}</p>;
  return (
    <div className="flex flex-col gap-5">
      {items.length === 0 && <p className="text-[13px] text-ink-2">{t("pj.activity.empty", { n: days })}</p>}
      {byDay.map(([d, list]) => (
        <section key={d} aria-label={dayLabel(d)}>
          <h4 className="sticky top-0 z-[1] bg-surface/95 pb-1 text-xs font-medium text-ink-2 first-letter:uppercase">{dayLabel(d)}</h4>
          <ul className="flex flex-col">
            {list.map((i, n) => {
              const body = (
                <>
                  <span className="grid h-5 w-5 place-items-center text-ink-2" aria-hidden>
                    {KIND_ICON[i.kind]}
                  </span>
                  <span className="min-w-0">
                    <span className="text-[13px] break-words">{i.title}</span>
                    <span className="block truncate text-xs text-ink-3">
                      {[t(`pj.activity.kind.${i.kind}`), i.repo ? repoName(i.repo) : null, i.who, i.ref && !i.ref.startsWith("T-") ? null : i.ref].filter(Boolean).join(" · ")}
                    </span>
                  </span>
                </>
              );
              const cls = "grid grid-cols-[20px_minmax(0,1fr)] items-start gap-2.5 rounded px-1.5 py-1.5 -mx-1.5";
              return (
                <li key={`${i.kind}${i.at}${n}`}>
                  {i.ref?.startsWith("T-") ? (
                    <TaskLink taskRef={i.ref} className={`${cls} hover:bg-raised`}>
                      {body}
                    </TaskLink>
                  ) : i.url ? (
                    <a href={i.url} target="_blank" rel="noreferrer" className={`${cls} hover:bg-raised`}>
                      {body}
                    </a>
                  ) : (
                    <div className={cls}>{body}</div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      {days < 90 && (
        <button type="button" className="btn self-start" onClick={() => setDays(90)}>
          {t("pj.activity.more")}
        </button>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ Zeptej se */

function Ask({ p }: { p: Project }) {
  const [q, setQ] = useState("");
  const [effort, setEffort] = useState<1 | 2>(2);
  const [busy, setBusy] = useState(false);
  const [a, setA] = useState<AskAnswer | null>(null);
  const send = (e: FormEvent) => {
    e.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    setA(null);
    projectsApi
      .ask(p.slug, q, effort)
      .then(setA, (err) => setA({ ok: false, error: err.message }))
      .finally(() => setBusy(false));
  };
  return (
    <div className="flex flex-col gap-4">
      <p className="text-[13px] text-ink-2">{t("pj.ask.hint")}</p>
      <form onSubmit={send} className="flex flex-col gap-2">
        <textarea
          className={area}
          rows={3}
          placeholder={t("pj.ask.placeholder")}
          aria-label={t("pj.ask.title")}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send(e);
          }}
        />
        <span className="flex flex-wrap items-center gap-2">
          <span role="radiogroup" aria-label="Effort" className="flex rounded-md border border-line p-0.5">
            {([1, 2] as const).map((n) => (
              <button key={n} type="button" role="radio" aria-checked={effort === n} onClick={() => setEffort(n)} className={`h-7 rounded px-2.5 text-xs ${effort === n ? "bg-raised text-ink" : "text-ink-2"}`}>
                {t(`pj.ask.effort${n}`)}
              </button>
            ))}
          </span>
          <button type="submit" className="btn-accent ml-auto h-9!" disabled={busy || !q.trim()}>
            <Send size={14} /> {busy ? t("pj.ask.asking") : t("pj.ask.send")}
          </button>
        </span>
      </form>
      {busy && <p className="breathe text-[13px] text-ink-2">{t("pj.ask.asking")}</p>}
      {a && !a.ok && <p className="text-[13px] break-words text-red-300">{a.error}</p>}
      {a?.ok && (
        <section className="flex flex-col gap-3 rounded-lg border border-line bg-raised/40 p-4">
          {a.insufficient_evidence && <p className="text-xs text-amber-200">{t("pj.ask.weak")}</p>}
          <Markdown text={a.answer ?? ""} />
          {!!a.citations?.length && (
            <div className="flex flex-col gap-1 border-t border-line pt-2">
              <span className="text-xs text-ink-2">{t("pj.ask.sources")}</span>
              {a.citations.slice(0, 8).map((c, i) => (
                <a key={i} href={c.url} target="_blank" rel="noreferrer" className="truncate text-xs text-accent hover:underline">
                  [{c.n ?? i + 1}] {c.title ?? c.url}
                </a>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ the page */

type Tab = "overview" | "files" | "tasks" | "people" | "activity" | "ask";
const TAB_IDS: Tab[] = ["overview", "files", "tasks", "people", "activity", "ask"];

let actorsCache: Promise<Actor[]> | null = null;

function ProjectDetail({ slug }: { slug: string }) {
  const [p, setP] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [actors, setActors] = useState<Actor[]>([]);
  const initial = new URLSearchParams(window.location.search).get("tab") as Tab | null;
  const [tab, setTabState] = useState<Tab>(initial && TAB_IDS.includes(initial) ? initial : "overview");
  const sheetOrder = useSheetOrder();
  const setTab = (x: Tab) => {
    setTabState(x);
    const u = new URL(window.location.href);
    if (x === "overview") u.searchParams.delete("tab");
    else u.searchParams.set("tab", x);
    window.history.replaceState(window.history.state, "", u.toString());
  };
  const load = useCallback(() => projectsApi.get(slug).then((x) => (setP(x), setError(null)), (e) => setError(e.message)), [slug]);
  useEffect(() => {
    setP(null);
    setSummary(null);
    load();
  }, [load]);
  // Tasks change in the panel: reload when it closes.
  useEffect(() => {
    const on = () => load();
    window.addEventListener("pos:tasks", on);
    return () => window.removeEventListener("pos:tasks", on);
  }, [load]);
  useEffect(() => {
    if (editing && !actors.length) (actorsCache ??= tasksApi.actors().catch(() => [])).then(setActors);
  }, [editing, actors.length]);
  const refreshSummary = useCallback(
    (force = false) => {
      setSummaryLoading(true);
      fetch(`/api/projects/${slug}/summary?generate=true${force ? "&force=true" : ""}`, { credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : null))
        .then((s) => s && setSummary(s))
        .finally(() => setSummaryLoading(false));
    },
    [slug],
  );
  useEffect(() => {
    if (!p) return;
    let alive = true;
    projectsApi.summary(p.slug, false).then((s) => {
      if (!alive) return;
      setSummary(s);
      if (!s.fresh) refreshSummary();
    }, () => undefined);
    return () => {
      alive = false;
    };
  }, [p?.slug, p?.updated_at, p?.tasks?.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async (c: Record<string, unknown>) => {
    try {
      const x = await projectsApi.update(slug, c);
      setP(x);
      toast(t("pj.saved"));
      return x;
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
      throw e;
    }
  };

  if (!p)
    return error ? (
      <p className="text-xs break-words text-red-400">{error}</p>
    ) : (
      <div className="flex flex-col gap-4" aria-busy="true">
        <span className="h-7 w-2/3 rounded bg-line" />
        <span className="h-4 w-1/3 rounded bg-line" />
        <span className="mt-6 h-24 w-full rounded bg-line/60" />
      </div>
    );

  const meta = editing ? <MetaEdit p={p} actors={actors} save={(c) => void save(c).catch(() => undefined)} onDone={() => setEditing(false)} /> : <Meta p={p} onEdit={() => setEditing(true)} />;
  const TABS: { id: Tab; text: string; n?: number }[] = [
    { id: "overview", text: t("pj.tab.overview") },
    { id: "files", text: t("pj.tab.files") },
    { id: "tasks", text: t("pj.tab.tasks"), n: p.total - p.counts.done },
    { id: "people", text: t("pj.tab.people"), n: p.people?.length },
    { id: "activity", text: t("pj.tab.activity") },
    { id: "ask", text: t("pj.tab.ask") },
  ];
  void sheetOrder;

  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-6">
      <Link to="/projects" className="flex items-center gap-1 self-start text-xs text-ink-2 hover:text-accent">
        <ArrowLeft size={12} /> {t("pj.back")}
      </Link>
      {/* Header */}
      <header className="flex flex-col gap-3.5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start">
          <h1 id="pj-title" className="min-w-0 flex-1 text-[24px] leading-tight font-normal tracking-[-0.01em] break-words sm:text-[28px]">
            {p.name}
          </h1>
          {p.can_edit && (
            <button type="button" className="btn h-9! shrink-0 self-start" onClick={() => setEditing(!editing)}>
              <Pencil size={14} /> {t("pj.edit")}
            </button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <ProjectChip status={p.status} size="md" />
          <span className="flex items-center gap-2 text-[13px]">
            <Avatar type={kindOf(p.lead_kind)} name={p.lead_name} size={26} />
            <span className="flex flex-col leading-tight">
              <span>{p.lead_name ? who(p.lead_name) : t("pj.no_lead")}</span>
              <span className="text-xs text-ink-2">{t("pj.lead")}</span>
            </span>
          </span>
          {(p.info.start_date || p.due) && (
            <span className="flex flex-col text-[13px] leading-tight">
              <span>{t("pj.dates", { start: p.info.start_date ? day(p.info.start_date) : "—", target: p.due ? day(p.due) : "—" })}</span>
              <span className="text-xs text-ink-2">
                {t("pj.start")} → {t("pj.target").toLowerCase()}
              </span>
            </span>
          )}
          <Progress p={p} wide />
        </div>
        <QuickLinks p={p} />
      </header>

      <details className="rounded-lg border border-line px-4 py-2 lg:hidden" open={editing || undefined}>
        <summary className="cursor-pointer py-1 text-[13px] text-ink-2">{t("pj.details_mobile")}</summary>
        <div className="pt-2">{meta}</div>
      </details>

      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="flex min-w-0 flex-col gap-6">
          <SummaryBox
            summary={summary}
            loading={summaryLoading}
            onRefresh={() => refreshSummary(true)}
            mine={(p.tasks ?? []).filter((x) => x.status !== "done" && (x.assignee_name === "Owner" || (x.status === "review" && x.reviewer_name === "Owner")))}
          />
          <RealityBox slug={p.slug} />
          <div className="flex min-w-0 flex-col">
            <div
              role="tablist"
              aria-label={t("pj.tabs")}
              className="flex gap-1 overflow-x-auto border-b border-line"
              onKeyDown={(e) => {
                const i = TABS.findIndex((x) => x.id === tab);
                const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
                if (!d) return;
                e.preventDefault();
                const next = TABS[(i + d + TABS.length) % TABS.length].id;
                setTab(next);
                document.getElementById(`pjtab-${next}`)?.focus();
              }}
            >
              {TABS.map((x) => (
                <button
                  key={x.id}
                  id={`pjtab-${x.id}`}
                  role="tab"
                  type="button"
                  aria-selected={tab === x.id}
                  aria-controls={`pjpanel-${x.id}`}
                  tabIndex={tab === x.id ? 0 : -1}
                  onClick={() => setTab(x.id)}
                  className={`-mb-px flex h-11 shrink-0 items-center gap-1.5 border-b-2 px-3 text-sm ${
                    tab === x.id ? "border-accent text-ink" : "border-transparent text-ink-2 hover:text-ink"
                  } ${x.id === "ask" ? "ml-auto" : ""}`}
                >
                  {x.id === "ask" && <Sparkles size={13} aria-hidden />}
                  {x.text}
                  {x.n ? <span className="rounded-full bg-raised px-1.5 text-xs text-ink-2 tabular-nums">{x.n}</span> : null}
                </button>
              ))}
            </div>
            <div id={`pjpanel-${tab}`} role="tabpanel" aria-labelledby={`pjtab-${tab}`} className="pt-5">
              {tab === "overview" && <Overview p={p} save={save} onChange={load} />}
              {tab === "files" && <Files p={p} save={save} />}
              {tab === "tasks" && <Tasks p={p} onChange={load} />}
              {tab === "people" && <People p={p} save={save} onChange={load} />}
              {tab === "activity" && <Activity p={p} />}
              {tab === "ask" && <Ask p={p} />}
            </div>
          </div>
        </div>
        <aside className="hidden min-w-0 lg:block">
          <div className="sticky top-4">{meta}</div>
        </aside>
      </div>
    </div>
  );
}

export default function Projects() {
  const { slug } = useParams();
  return slug ? <ProjectDetail key={slug} slug={slug} /> : <ProjectList />;
}
