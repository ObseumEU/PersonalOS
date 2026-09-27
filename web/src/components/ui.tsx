import { ArrowRight, Orbit } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { LOCALE, t } from "../i18n";

export function Panel({
  title,
  right,
  mock,
  className = "",
  bodyClassName = "",
  children,
}: {
  /** Deprecated: the decorative "FIG. 1" labels are gone; kept so old call sites compile. */
  fig?: string;
  title?: string;
  right?: ReactNode;
  /** Red mock dot next to the title; a string says why (tooltip). See docs/MOCK-STATUS.md. */
  mock?: boolean | string;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}) {
  return (
    <section className={`panel fade-in flex min-h-0 flex-col ${className}`}>
      {title && (
        <div className="flex items-baseline gap-2.5 border-b border-line px-4 py-3">
          <h2 className="text-sm font-medium">{title}</h2>
          {mock && <MockDot why={typeof mock === "string" ? mock : undefined} />}
          {right && <span className="ml-auto min-w-0 truncate pl-3 text-xs text-ink-2">{right}</span>}
        </div>
      )}
      <div className={`min-h-0 flex-1 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

export const MOCK_TITLE = t("misc.mock_title");

/**
 * Small red dot on anything that is mock (sample data, unwired control, planned
 * screen). Real, working parts carry no dot. The list lives in docs/MOCK-STATUS.md;
 * remove the dot there and here when the part is wired to real data.
 */
export function MockDot({ why, className = "" }: { why?: string; className?: string }) {
  const label = why ? `${t("misc.mock_title")}: ${why}` : MOCK_TITLE;
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={`inline-block h-[7px] w-[7px] shrink-0 rounded-full bg-red-500 ${className}`}
    />
  );
}

/** Marks placeholder content until real data arrives. */
export function SampleBadge({ why }: { why?: string }) {
  return <MockDot why={why} />;
}

function useNow() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

let healthState: { ok: boolean | null; at: number } = { ok: null, at: 0 };

/** "API online" from a live /api/health check (shared by every header, every 30 s). */
function ApiStatus() {
  const [ok, setOk] = useState<boolean | null>(healthState.ok);
  useEffect(() => {
    let alive = true;
    const check = () => {
      if (Date.now() - healthState.at < 25000 && healthState.ok !== null) return setOk(healthState.ok);
      fetch("/api/health", { credentials: "same-origin" })
        .then((r) => r.ok, () => false)
        .then((v) => {
          healthState = { ok: v, at: Date.now() };
          if (alive) setOk(v);
        });
    };
    check();
    const t = setInterval(check, 30000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  return (
    <span className="flex items-center gap-2 text-sm">
      <span className={`h-[7px] w-[7px] rounded-full ${ok === false ? "bg-red-400" : ok ? "sonar bg-accent" : "bg-dim"}`} />
      {ok === false ? t("api.down") : ok ? t("api.online") : t("api.checking")}
    </span>
  );
}

export function PageHeader({ kicker, title, sub }: { kicker: string; title: string; sub?: ReactNode }) {
  const now = useNow();
  const hm = now.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  const s = String(now.getSeconds()).padStart(2, "0");
  return (
    <header className="flex flex-wrap items-end gap-6 border-b border-line pb-4">
      <div className="flex min-w-0 flex-col gap-2">
        <span className="text-xs font-medium text-ink-2">{kicker}</span>
        <h1 className="text-3xl leading-tight font-light tracking-[-0.03em] sm:text-[40px]">{title}</h1>
        {sub && <p className="text-sm text-ink-2">{sub}</p>}
      </div>
      <div className="ml-auto hidden items-end gap-7 text-right sm:flex">
        <div className="flex flex-col gap-1">
          <span className="text-xs text-ink-2">{t("time.local")}</span>
          <span className="font-mono text-[22px]">
            {hm}
            <span className="text-ink-3">:{s}</span>
          </span>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-ink-2">{t("time.status")}</span>
          <ApiStatus />
        </div>
      </div>
    </header>
  );
}

export function AskBox({
  placeholder,
  id,
  onAsk,
  busy = false,
  defaultValue = "",
}: {
  placeholder: string;
  id: string;
  /** Without it, asking opens the Assistant with the question. */
  onAsk?: (question: string) => void;
  busy?: boolean;
  defaultValue?: string;
}) {
  const navigate = useNavigate();
  return (
    <form
      className="flex h-[52px] items-center gap-3 rounded-md border border-line bg-bg pr-1.5 pl-4 focus-within:border-accent"
      onSubmit={(e) => {
        e.preventDefault();
        const q = String(new FormData(e.currentTarget).get("q") ?? "").trim();
        if (!q || busy) return;
        if (onAsk) onAsk(q);
        else navigate(`/assistant?q=${encodeURIComponent(q)}`);
      }}
    >
      <Orbit size={18} strokeWidth={1.5} className="shrink-0 text-accent" />
      <label htmlFor={id} className="sr-only">
        {t("misc.ask_label")}
      </label>
      <input
        id={id}
        name="q"
        defaultValue={defaultValue}
        placeholder={placeholder}
        className="min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-ink-3"
      />
      <button
        type="submit"
        disabled={busy}
        title={t("misc.ask_title")}
        className="flex h-[38px] items-center gap-1.5 rounded border border-accent bg-accent/10 px-3.5 text-[13px] font-medium text-accent transition hover:bg-accent/20"
      >
        {busy ? t("misc.asking") : t("misc.ask")} <ArrowRight size={14} />
      </button>
    </form>
  );
}

export function Legend({ items }: { items: [string, string][] }) {
  return (
    <div className="flex flex-wrap gap-4">
      {items.map(([color, label]) => (
        <span key={label} className="cap flex items-center gap-1.5">
          <span className="h-[7px] w-[7px] rounded-full" style={{ background: color }} />
          {label}
        </span>
      ))}
    </div>
  );
}
