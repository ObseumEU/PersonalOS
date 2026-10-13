import { KeyRound, LockKeyhole, Search, ShieldAlert, Trash2, X } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api";
import secretsDict from "../i18n/cs/secrets";
import { ago, register, t } from "../i18n/core";
import { type Secret, type SecretsOverview, matchSecret, secretsApi, sortSecrets } from "../secretsApi";
import { confirmDialog, toast } from "./overlay";

// The page's words register with it (the installed app loads them only when Tajemství opens).
register(secretsDict);

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function sourceLabel(src: string | null): string {
  return src ? t(`sec.src.${src}`) : "";
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 gap-2 text-[13px] leading-snug">
      <span className="w-24 shrink-0 text-ink-2">{label}</span>
      <span className="min-w-0 flex-1 break-words">{children}</span>
    </div>
  );
}

function SecretCard({ s, mobile, reload }: { s: Secret; mobile: boolean; reload: () => void }) {
  const [busy, setBusy] = useState(false);
  const run = async (p: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try {
      await p();
      toast(ok);
    } catch (e) {
      toast(errText(e), { error: true });
    } finally {
      setBusy(false);
      reload();
    }
  };
  const revoke = async (agentId: number, agent: string) => {
    const why = await confirmDialog({ title: t("sec.revoke_title", { agent, name: s.name }), reason: t("sec.revoke_reason"), confirm: t("sec.revoke"), danger: true });
    if (why === null) return;
    run(() => secretsApi.revoke(s.name, agentId, why), t("sec.revoked", { agent, name: s.name }));
  };
  const remove = async () => {
    const why = await confirmDialog({
      title: t("sec.delete_title", { name: s.name }),
      body: t(s.origin === "operator" ? "sec.delete_operator_body" : "sec.delete_body"),
      reason: t("sec.delete_reason"),
      confirm: t("sec.delete"),
      danger: true,
    });
    if (why === null) return;
    run(() => secretsApi.remove(s.name, why), t("sec.deleted", { name: s.name }));
  };
  const flip = () => run(() => secretsApi.sensitivity(s.name, s.sensitivity === "high" ? "normal" : "high"), t("sec.sens_set", { name: s.name }));
  const high = s.sensitivity === "high";
  const btn = mobile ? "h-11 px-4 text-[15px]" : "h-8 px-3 text-xs";
  return (
    <article className={`panel flex min-w-0 flex-col gap-3 p-4 ${high ? "border-amber-400/50!" : ""}`}>
      <header className="flex min-w-0 flex-wrap items-center gap-2">
        {high ? <ShieldAlert size={16} className="shrink-0 text-amber-300" /> : <KeyRound size={16} className="shrink-0 text-accent" />}
        <h3 className="min-w-0 font-mono text-sm break-all">{s.name}</h3>
        <span className={`rounded-full border px-2 py-0.5 text-[11px] ${high ? "border-amber-400/60 text-amber-300" : "border-line text-ink-2"}`} title={high ? t("sec.high_hint") : undefined}>
          {high ? t("sec.high") : t("sec.normal")}
        </span>
        <span className="rounded-full border border-line px-2 py-0.5 text-[11px] text-ink-2">{t(`sec.origin.${s.origin}`)}</span>
        {!s.stored && <span className="text-[11px] text-red-400">{t("sec.not_stored")}</span>}
      </header>
      {s.description && <p className="text-[13px] leading-snug">{s.description}</p>}
      <div className="flex flex-col gap-1">
        <Row label={t("sec.by")}>
          {s.creator ?? t("sec.origin.operator")} · {t("sec.created", { when: ago(s.created_at) })}
          {s.rotated_at ? ` · ${t("sec.rotated", { when: ago(s.rotated_at) })}` : ""}
        </Row>
        <Row label={t("sec.team")}>
          {s.team || t("sec.no_team")}
          {s.project ? ` · ${t("sec.project")}: ${s.project}` : ""}
        </Row>
        {s.purpose && <Row label={t("sec.purpose")}>{s.purpose}</Row>}
        {s.hosts.length > 0 && <Row label={t("sec.hosts")}><span className="font-mono text-xs">{s.hosts.join(", ")}</span></Row>}
        <Row label={t("sec.fields")}>
          {s.fields.map((f) => (
            <span key={f.name} className="mr-2 inline-block">
              <code className="text-xs">{f.name}</code>
              {f.source && <span className="text-ink-2"> ({sourceLabel(f.source)})</span>}
            </span>
          ))}
        </Row>
        <Row label={t("sec.last_use")}>
          {s.last_use ? (
            <>
              {ago(s.last_use.at)} · {s.last_use.agent ?? "PersonalOS"} · {s.last_use.tool}
              {!s.last_use.ok && <span className="text-red-400"> · {t("sec.refused")}</span>}
            </>
          ) : (
            <span className="text-ink-2">{t("sec.never_used")}</span>
          )}
          {s.uses_7d > 0 && <span className="text-ink-2"> · {t("sec.uses_7d", { n: s.uses_7d })}</span>}
        </Row>
      </div>
      <div className="flex min-w-0 flex-col gap-1.5">
        <span className="text-xs text-ink-2">{t("sec.holders")}</span>
        <div className="flex flex-wrap gap-1.5">
          {s.holders.length === 0 && <span className="text-[13px] text-ink-2">{t("sec.nobody")}</span>}
          {s.holders.map((h) => (
            <span key={h.agent_id} className="inline-flex items-center gap-1 rounded-full border border-line bg-raised py-0.5 pr-1 pl-2.5 text-[13px]" title={h.granted_by.join(", ")}>
              {h.name}
              <button
                disabled={busy}
                onClick={() => revoke(h.agent_id, h.name)}
                aria-label={`${t("sec.revoke")} ${h.name}`}
                className={`grid place-items-center rounded-full text-ink-2 hover:text-red-400 disabled:opacity-40 ${mobile ? "h-9 w-9" : "h-5 w-5"}`}
              >
                <X size={mobile ? 16 : 12} />
              </button>
            </span>
          ))}
        </div>
      </div>
      {s.pending.map((p) => (
        <p key={p.approval_id} className="rounded-md border border-amber-400/40 px-3 py-2 text-[13px] text-amber-200">
          {t("sec.pending", { from: p.from ?? "?", to: p.to.join(", ") })}
          {p.why ? ` — ${p.why}` : ""}{" "}
          <Link to={mobile ? `/m/needs` : `/approvals#a${p.approval_id}`} className="font-medium text-accent hover:underline">
            {t("sec.pending_open")}
          </Link>
        </p>
      ))}
      <footer className="flex flex-wrap gap-2">
        <button disabled={busy} onClick={flip} className={`inline-flex items-center gap-1.5 rounded-md border border-line ${btn} hover:border-accent disabled:opacity-40`}>
          <ShieldAlert size={14} /> {high ? t("sec.make_normal") : t("sec.make_high")}
        </button>
        <button disabled={busy} onClick={remove} className={`inline-flex items-center gap-1.5 rounded-md border border-red-400/40 text-red-300 ${btn} hover:border-red-400 disabled:opacity-40`}>
          <Trash2 size={14} /> {t("sec.delete")}
        </button>
      </footer>
    </article>
  );
}

/** Tajemství: every secret with its creator, holders, last use and sensitivity. Never a value. */
export default function SecretsView({ mobile = false }: { mobile?: boolean }) {
  const [v, setV] = useState<SecretsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const load = useCallback(() => {
    secretsApi.overview().then(
      (x) => {
        setV(x);
        setError(null);
      },
      (e) => setError(e instanceof ApiError && e.status === 403 ? t("sec.owner_only") : t("sec.loading_error", { error: errText(e) })),
    );
  }, []);
  useEffect(load, [load]);
  const shown = useMemo(() => (v ? sortSecrets(v.secrets).filter((s) => matchSecret(s, q)) : []), [v, q]);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <p className="flex gap-2 rounded-md border border-line px-3 py-2.5 text-[13px] leading-relaxed text-ink-2">
        <LockKeyhole size={16} className="mt-0.5 shrink-0 text-accent" />
        <span>{t("sec.never")}</span>
      </p>
      {v && !v.store.enabled && <p className="panel border-amber-400/60! px-4 py-3 text-sm text-amber-300">{t("sec.store_off", { reason: v.store.reason ?? "" })}</p>}
      {error && <p className="text-sm break-words text-red-400">{error}</p>}
      {v && (
        <div className="flex flex-wrap items-center gap-3">
          <label className="relative min-w-0 flex-1">
            <Search size={14} className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-ink-2" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("sec.search")}
              className={`w-full rounded-md border border-line bg-bg pr-2 pl-8 outline-none focus:border-accent ${mobile ? "h-11 text-[16px]" : "h-9 text-sm"}`}
            />
          </label>
          <span className="text-xs text-ink-2">{t("sec.count", { n: v.secrets.length, limit: v.per_agent_limit })}</span>
        </div>
      )}
      {!v && !error && <p className="text-xs text-ink-2">{t("act.loading")}</p>}
      {v && v.secrets.length === 0 && <p className="panel px-4 py-3 text-sm text-ink-2">{t("sec.empty")}</p>}
      {v && v.secrets.length > 0 && shown.length === 0 && <p className="text-sm text-ink-2">{t("sec.none_found")}</p>}
      <div className={`grid min-w-0 grid-cols-1 gap-3 ${mobile ? "" : "lg:grid-cols-2"}`}>
        {shown.map((s) => (
          <SecretCard key={s.name} s={s} mobile={mobile} reload={load} />
        ))}
      </div>
    </div>
  );
}
