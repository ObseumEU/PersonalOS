import { Check, X } from "lucide-react";
import { TaskLink } from "../taskSheet";
import { useCallback, useEffect, useState } from "react";
import { type Approval, agentsApi } from "../agentsApi";
import { outboundApi } from "../outboundApi";
import { ActorChip } from "../components/agents/bits";
import ApprovalBody, { approveLabel } from "../components/ApprovalBody";
import { confirmDialog } from "../components/overlay";
import { PageHeader, Panel } from "../components/ui";
import { ago, label, t } from "../i18n";
import { useLiveReload } from "../liveStream";

function Item({ a, onDone }: { a: Approval; onDone: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const action = a.view?.title || label("approval", a.action);
  async function decide(approve: boolean) {
    let comment: string | undefined;
    if (!approve) {
      const why = await confirmDialog({ title: t("appr.reject_title", { action }), confirm: t("act.reject"), reason: t("appr.reject_reason"), danger: true });
      if (why === null) return;
      comment = why || undefined;
    }
    try {
      await agentsApi.decide(a.id, approve, comment);
      window.dispatchEvent(new Event("pos:approvals"));
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  const shot = typeof a.details?.screenshot === "string" ? (a.details.screenshot as string) : null;
  const details = Object.entries(a.details ?? {}).filter(
    ([k]) => k !== "screenshot" && !(typeof a.details?.text === "string" && (k === "payload" || k === "image_url")),
  );
  const result = a.result;
  return (
    <div id={`a${a.id}`} className="panel fade-in flex min-w-0 flex-col gap-3 p-4">
      <span className="flex flex-wrap items-center gap-2">
        <ActorChip a={{ kind: a.requested_by_kind, name: a.requested_by_name, is_owner: false }} />
        {a.status === "pending" ? (
          <span className="text-xs text-amber-300">{t("appr.needs_you")}</span>
        ) : (
          <span className="text-xs text-ink-2">{label("appr.status", a.status)}</span>
        )}
        <span className="ml-auto text-xs text-ink-2">
          #{a.id} · {ago(a.created_at)}
        </span>
      </span>
      <span className="text-base break-words">{action}</span>
      {a.view && <ApprovalBody v={a.view} />}
      {!a.view && details.length > 0 && (
        <div className="grid grid-cols-1 gap-x-3 gap-y-1 rounded border border-line bg-bg p-3 text-xs sm:grid-cols-[110px_minmax(0,1fr)]">
          {details.map(([k, v]) => (
            <div key={k} className="contents">
              <span className="text-ink-2">{label("appr.field", k)}</span>
              <span className="min-w-0 break-words text-ink">{typeof v === "string" ? v : JSON.stringify(v)}</span>
            </div>
          ))}
        </div>
      )}
      {shot && (
        <a href={`/api/browser/screenshots/${shot}`} target="_blank" rel="noreferrer" title={t("appr.shot_title")}>
          <img src={`/api/browser/screenshots/${shot}`} alt={t("appr.shot_alt")} className="max-h-64 max-w-full rounded border border-line" />
        </a>
      )}
      {a.task_ref && (
        <TaskLink taskRef={a.task_ref} className="text-xs text-accent hover:underline">
          {t("appr.for_task", { ref: a.task_ref })}
        </TaskLink>
      )}
      {a.status === "pending" && (
        <span className="flex flex-wrap gap-2">
          <button className="btn-accent" onClick={() => decide(true)}>
            <Check size={14} /> {approveLabel(a.view)}
          </button>
          <button className="btn" onClick={() => decide(false)}>
            <X size={14} /> {t("act.reject")}
          </button>
        </span>
      )}
      {a.comment && <span className="text-xs break-words text-ink-2">„{a.comment}“</span>}
      {result && (
        <span className={`text-xs break-words ${result.status === "sent" ? "text-accent" : result.status === "failed" ? "text-red-400" : "text-ink-2"}`}>
          {t("appr.result", { status: label("appr.result", result.status) })}
          {result.owner_task ? t("appr.by_hand", { task: result.owner_task }) : ""}
          {result.error ? ` · ${result.error}` : ""}
        </span>
      )}
      {a.action === "linkedin.post" && result?.status === "ready_to_publish" && (
        <button
          className="btn-accent self-start"
          onClick={() =>
            outboundApi.publishLinkedIn(a.id).then(onDone, (e) => setError(e instanceof Error ? e.message : String(e)))
          }
        >
          {t("outbound.linkedin_publish")}
        </button>
      )}
      {error && <span className="text-xs break-words text-red-400">{error}</span>}
    </div>
  );
}

export default function Approvals() {
  const [items, setItems] = useState<Approval[] | null>(null);
  const [all, setAll] = useState(false);
  const load = useCallback(() => agentsApi.approvals(all ? "all" : "pending").then(setItems), [all]);
  useEffect(() => {
    load();
  }, [load]);
  useLiveReload(["approval"], load);

  return (
    <div className="flex min-w-0 flex-col gap-5">
      <PageHeader kicker={t("appr.kicker")} title={t("nav.approvals")} sub={t("appr.sub")} />
      <div className="flex gap-2">
        <button className={all ? "btn" : "btn-accent"} onClick={() => setAll(false)}>
          {t("appr.waiting")}
        </button>
        <button className={all ? "btn-accent" : "btn"} onClick={() => setAll(true)}>
          {t("appr.history")}
        </button>
      </div>
      <Panel
        title={all ? t("appr.all") : t("appr.waiting_for_you")}
        right={`${items?.length ?? 0}`}
        bodyClassName="grid gap-3 p-3 md:grid-cols-2 xl:grid-cols-3"
      >
        {items?.length === 0 && <p className="p-3 text-sm text-ink-2">{t("appr.empty")}</p>}
        {items?.map((a) => (
          <Item key={a.id} a={a} onDone={load} />
        ))}
      </Panel>
    </div>
  );
}
