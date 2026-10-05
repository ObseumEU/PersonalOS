import { Archive, Ban, Check, ChevronLeft, Pencil, Plus } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api, getMe } from "../api";
import { confirmDialog, toast } from "../components/overlay";
import { PageHeader } from "../components/ui";
import { type GoalForm, formOf, goalBody } from "../goalsForm";
import goals from "../i18n/cs/goals";
import { label, register, t } from "../i18n/core";
import type { Goal } from "../reportsApi";

// The installed app (/m) registers only its first screens' strings: this page brings its own.
register(goals);

const post = <T,>(path: string, body: unknown = {}) => api<T>(path, { method: "POST", body: JSON.stringify(body) });

const goalsApi = {
  list: (status: string) => api<Goal[]>(`/api/goals?status=${status}`),
  create: (body: Record<string, unknown>) => post<Goal>("/api/goals", body),
  update: (id: number, body: Record<string, unknown>) => api<Goal>(`/api/goals/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  veto: (id: number, note: string) => post<Goal>(`/api/goals/${id}/veto`, { note }),
  archive: (id: number) => post<Goal>(`/api/goals/${id}/archive`),
};

type Filter = "active" | "proposed" | "all";
const FILTERS: Filter[] = ["active", "proposed", "all"];
const STATUSES = ["proposed", "active", "paused", "done", "dropped"];
const num = (v: number | null | undefined) => (v == null ? "?" : Number.isInteger(v) ? String(v) : v.toFixed(1));
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const input = "min-w-0 rounded-md border border-line bg-bg px-2.5 py-1.5 text-[14px] outline-none focus:border-accent";

function Field({ label: text, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <label className={`flex min-w-0 flex-col gap-1 text-xs text-ink-2 ${wide ? "sm:col-span-2" : ""}`}>
      {text}
      {children}
    </label>
  );
}

function Editor({ goal, all, onDone, onCancel }: { goal?: Goal; all: Goal[]; onDone: (g: Goal) => void; onCancel: () => void }) {
  const [before] = useState(() => formOf(goal));
  const [f, setF] = useState<GoalForm>(before);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: keyof GoalForm) => (e: { target: { value: string } }) => setF((x) => ({ ...x, [k]: e.target.value }));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const body = goalBody(f, goal ? before : undefined);
    if ("invalid" in body) return setError(t(`gl.f.${body.invalid}`));
    if (goal && !Object.keys(body).length) return onCancel();
    setBusy(true);
    setError(null);
    try {
      const g = goal ? await goalsApi.update(goal.id, body) : await goalsApi.create(body);
      toast(goal ? t("gl.saved") : t("gl.created", { title: g.title }));
      onDone(g);
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="grid grid-cols-1 gap-3 border-b border-line bg-raised/30 px-4 py-4 sm:grid-cols-2">
      <Field label={t("gl.f.title")} wide>
        <input autoFocus required value={f.title} onChange={set("title")} placeholder={t("gl.f.title_ph")} className={input} />
      </Field>
      <Field label={t("gl.f.why")} wide>
        <textarea rows={2} value={f.why} onChange={set("why")} className={`${input} resize-y`} />
      </Field>
      <Field label={t("gl.f.target")} wide>
        <input value={f.target} onChange={set("target")} placeholder={t("gl.f.target_ph")} className={input} />
      </Field>
      <Field label={t("gl.f.owner")}>
        <input value={f.owner} onChange={set("owner")} className={input} />
      </Field>
      <Field label={t("gl.f.due")}>
        <input type="date" value={f.due} onChange={set("due")} className={input} />
      </Field>
      <Field label={t("gl.f.status")}>
        <select value={f.status} onChange={set("status")} className={input}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {label("gl.status", s)}
            </option>
          ))}
        </select>
      </Field>
      <Field label={t("gl.f.progress")}>
        <input inputMode="numeric" value={f.progress} onChange={set("progress")} className={input} />
      </Field>
      <Field label={t("gl.f.parent")} wide>
        <select value={f.parent_id} onChange={set("parent_id")} className={input}>
          <option value="">{t("gl.f.parent_none")}</option>
          {all
            .filter((g) => g.id !== goal?.id)
            .map((g) => (
              <option key={g.id} value={g.id}>
                {g.title}
              </option>
            ))}
        </select>
      </Field>
      <fieldset className="grid grid-cols-2 gap-3 sm:col-span-2 sm:grid-cols-4">
        <legend className="mb-1 text-xs font-medium text-ink-2">{t("gl.f.numbers")}</legend>
        <Field label={t("gl.f.metric")}>
          <input value={f.metric} onChange={set("metric")} className={input} />
        </Field>
        <Field label={t("gl.f.baseline")}>
          <input inputMode="decimal" value={f.baseline} onChange={set("baseline")} className={input} />
        </Field>
        <Field label={t("gl.f.current")}>
          <input inputMode="decimal" value={f.current} onChange={set("current")} className={input} />
        </Field>
        <Field label={t("gl.f.target_value")}>
          <input inputMode="decimal" value={f.target_value} onChange={set("target_value")} className={input} />
        </Field>
      </fieldset>
      <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
        {error && <span className="min-w-0 flex-1 text-xs break-words text-red-400">{error}</span>}
        <button type="button" className="btn ml-auto" onClick={onCancel}>
          {t("gl.cancel")}
        </button>
        <button className="btn-accent" disabled={busy || !f.title.trim()}>
          {goal ? t("gl.save") : t("gl.create")}
        </button>
      </div>
    </form>
  );
}

function GoalCard({ g, owner, onChange, onEdit }: { g: Goal; owner: boolean; onChange: () => void; onEdit: () => void }) {
  const act = async (p: Promise<unknown>, done: string) => {
    try {
      await p;
      toast(done);
      onChange();
    } catch (e) {
      toast(errText(e), { error: true });
    }
  };
  const pct = Math.max(0, Math.min(100, g.progress_effective ?? 0));
  const tone = g.status === "proposed" ? "text-amber-300" : g.status === "done" ? "text-emerald-300" : g.status === "dropped" ? "text-red-300" : "text-ink-2";
  const btn = "flex h-8 items-center gap-1.5 rounded-md border border-line px-2.5 text-[13px] text-ink-2 hover:border-accent/60 hover:text-ink";
  return (
    <li className="flex flex-col gap-2 border-b border-line px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="min-w-0 text-[15px] font-medium">{g.title}</span>
        <span className={`text-xs ${tone}`}>{label("gl.status", g.status)}</span>
        <span className="ml-auto font-mono text-sm tabular-nums">{pct} %</span>
      </div>
      <div className="h-1.5 rounded-[1px] bg-line" aria-hidden>
        <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
      </div>
      {g.target && <p className="text-[13px] text-ink">{g.target}</p>}
      {g.why && <p className="text-xs leading-relaxed text-ink-2">{g.why}</p>}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-2">
        <span>{g.owner_name ?? t("gl.no_owner")}</span>
        {g.due && <span>{t("gl.due", { due: g.due })}</span>}
        {g.metric && g.target_value != null && (
          <span>{t("gl.numbers", { metric: g.metric, current: num(g.current), baseline: num(g.baseline ?? 0), target: num(g.target_value) })}</span>
        )}
        {g.tasks.total > 0 && <span>{t("gl.tasks", { done: g.tasks.done, total: g.tasks.total })}</span>}
        {g.parent_title && <span>{t("gl.parent", { title: g.parent_title })}</span>}
      </div>
      {g.veto_note && <p className="text-xs text-red-300">{t("gl.veto_note", { note: g.veto_note })}</p>}
      <div className="flex flex-wrap gap-2">
        {owner && g.status === "proposed" && (
          <button type="button" className={btn} onClick={() => act(goalsApi.update(g.id, { status: "active" }), t("gl.confirmed"))}>
            <Check size={14} /> {t("gl.confirm")}
          </button>
        )}
        <button type="button" className={btn} onClick={onEdit}>
          <Pencil size={13} /> {t("gl.edit")}
        </button>
        {owner && g.status !== "dropped" && (
          <button
            type="button"
            className={`${btn} hover:border-red-400/60! hover:text-red-300!`}
            onClick={async () => {
              const note = await confirmDialog({ title: t("gl.veto_ask", { title: g.title }), body: t("gl.veto_body"), confirm: t("gl.veto"), danger: true, reason: t("gl.veto_reason") });
              if (note !== null) act(goalsApi.veto(g.id, note), t("gl.vetoed"));
            }}
          >
            <Ban size={13} /> {t("gl.veto")}
          </button>
        )}
        <button
          type="button"
          className={btn}
          onClick={async () => {
            const ok = await confirmDialog({ title: t("gl.archive_ask", { title: g.title }), body: t("gl.archive_body"), confirm: t("gl.archive") });
            if (ok !== null) act(goalsApi.archive(g.id), t("gl.archived"));
          }}
        >
          <Archive size={13} /> {t("gl.archive")}
        </button>
      </div>
    </li>
  );
}

/** Cíle: list, create, edit, archive; the owner confirms proposals and vetoes. /goals and /m/goals. */
export default function Goals() {
  const loc = useLocation();
  const navigate = useNavigate();
  const mobile = loc.pathname.startsWith("/m/");
  const [filter, setFilter] = useState<Filter>("active");
  const [items, setItems] = useState<Goal[] | null>(null);
  const [all, setAll] = useState<Goal[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [owner, setOwner] = useState(false);
  const [editing, setEditing] = useState<number | "new" | null>(null);

  useEffect(() => {
    getMe().then((m) => setOwner(!!m.is_owner), () => undefined);
  }, []);
  const load = useCallback(() => {
    goalsApi.list(filter).then(
      (x) => {
        setItems(x);
        setError(null);
      },
      (e) => setError(errText(e)),
    );
    // Parent choices: every goal, whatever the filter shows.
    goalsApi.list("all").then(setAll, () => undefined);
  }, [filter]);
  useEffect(load, [load]);

  const done = () => {
    setEditing(null);
    load();
  };

  return (
    <div className={`flex flex-col gap-4 ${mobile ? "px-3 pt-[calc(env(safe-area-inset-top)+12px)] pb-6" : ""}`}>
      {mobile ? (
        <header className="flex items-center gap-2">
          <button type="button" onClick={() => navigate("/m/more")} className="grid h-10 w-10 place-items-center rounded-lg text-ink-2 active:bg-raised" aria-label={t("gl.back")}>
            <ChevronLeft size={22} />
          </button>
          <h1 className="flex-1 text-xl font-medium">{t("gl.title")}</h1>
        </header>
      ) : (
        <PageHeader kicker={t("gl.kicker")} title={t("gl.title")} sub={t("gl.sub")} />
      )}
      <section className="panel flex min-w-0 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3">
          <div role="group" aria-label={t("gl.filter")} className="flex gap-1.5">
            {FILTERS.map((f) => (
              <button
                key={f}
                type="button"
                aria-pressed={filter === f}
                onClick={() => setFilter(f)}
                className={`h-8 rounded-full border px-3 text-[13px] ${filter === f ? "border-accent bg-accent/10 text-ink" : "border-line text-ink-2 hover:text-ink"}`}
              >
                {t(`gl.filter.${f}`)}
              </button>
            ))}
          </div>
          {editing !== "new" && (
            <button type="button" className="btn-accent ml-auto" onClick={() => setEditing("new")}>
              <Plus size={14} /> {t("gl.new")}
            </button>
          )}
        </div>
        {editing === "new" && <Editor all={all} onDone={done} onCancel={() => setEditing(null)} />}
        {!items && !error && <p className="p-6 text-sm text-ink-2">{t("gl.loading")}</p>}
        {error && <p className="p-4 text-sm break-words text-red-400">{t("gl.error", { error })}</p>}
        {items && items.length === 0 && <p className="p-8 text-center text-sm text-ink-2">{t(`gl.none.${filter}`)}</p>}
        <ul>
          {(items ?? []).map((g) =>
            editing === g.id ? (
              <li key={g.id}>
                <Editor goal={g} all={all} onDone={done} onCancel={() => setEditing(null)} />
              </li>
            ) : (
              <GoalCard key={g.id} g={g} owner={owner} onChange={load} onEdit={() => setEditing(g.id)} />
            ),
          )}
        </ul>
      </section>
    </div>
  );
}
