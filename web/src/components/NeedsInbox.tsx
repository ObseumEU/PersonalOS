import { AtSign, Check, CheckCheck, ExternalLink, HelpCircle, MessageSquare, ShieldCheck, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { agentsApi } from "../agentsApi";
import { chatApi } from "../chatApi";
import { ago, label, plural, t } from "../i18n";
import { type NeedsItem, dropNeedsItem, refreshNeedsMe, useNeedsMe } from "../needsMeApi";
import { tasksApi } from "../tasksApi";
import { toast } from "./overlay";
import { Panel } from "./ui";

const KIND = {
  approval: { Icon: ShieldCheck, key: "needs.kind.approval", cls: "text-amber-300" },
  ask: { Icon: HelpCircle, key: "needs.kind.ask", cls: "text-amber-300" },
  review: { Icon: CheckCheck, key: "needs.kind.review", cls: "text-accent" },
  mention: { Icon: AtSign, key: "needs.kind.mention", cls: "text-accent" },
} as const;

type Mode = null | "reply" | "reject" | "return";

function Item({ it }: { it: NeedsItem }) {
  const [mode, setMode] = useState<Mode>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const { Icon, key, cls } = KIND[it.kind];

  const run = async (p: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await p();
      dropNeedsItem(it.key);
      toast(done);
      window.dispatchEvent(new Event("pos:approvals"));
      refreshNeedsMe();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
    }
  };

  const submit = () => {
    const body = text.trim();
    if (mode === "reply" && !body) return;
    if (mode === "reply" && it.kind === "mention")
      return run(async () => {
        await chatApi.send(it.channel_id!, body, it.thread ?? null);
        await chatApi.read(it.channel_id!, it.id);
      }, t("needs.done.replied"));
    if (mode === "reply" && it.kind === "ask") return run(() => tasksApi.comment(it.ref!, body), t("needs.done.answered"));
    if (mode === "reject") return run(() => agentsApi.decide(it.id, false, body || undefined), t("needs.done.rejected"));
    if (mode === "return") return run(() => tasksApi.review(it.ref!, false, body || undefined), t("needs.done.returned"));
  };

  return (
    <li className="flex flex-col gap-2 border-b border-line px-4 py-3 last:border-0">
      <div className="flex min-w-0 items-start gap-3">
        <Icon size={18} strokeWidth={1.6} className={`mt-0.5 shrink-0 ${cls}`} aria-hidden />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className={`text-xs font-medium ${cls}`}>{t(key)}</span>
            {it.ref && <span className="font-mono text-xs text-ink-2">{it.ref}</span>}
            {it.blocking && <span className="text-xs text-amber-300">{t("needs.blocking")}</span>}
            <span className="text-xs text-ink-2">
              {it.from_name ? `${it.from_name} · ` : ""}
              {ago(it.at)}
            </span>
          </span>
          <Link to={it.link} className="text-sm break-words hover:text-accent">
            {it.kind === "approval" && it.action ? label("approval", it.action) : it.title}
          </Link>
          {it.detail && <p className="line-clamp-2 text-[13px] break-words text-ink-2">{it.detail}</p>}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2 pl-[30px]">
        {it.kind === "approval" && (
          <>
            <button className="btn-accent" disabled={busy} onClick={() => run(() => agentsApi.decide(it.id, true), t("needs.done.approved"))}>
              <Check size={14} /> {t("act.approve")}
            </button>
            <button className="btn" disabled={busy} onClick={() => setMode(mode === "reject" ? null : "reject")}>
              <X size={14} /> {t("act.reject")}
            </button>
          </>
        )}
        {it.kind === "review" && (
          <>
            <button className="btn-accent" disabled={busy} onClick={() => run(() => tasksApi.review(it.ref!, true), t("needs.done.accepted"))}>
              <Check size={14} /> {t("act.approve")}
            </button>
            <button className="btn" disabled={busy} onClick={() => setMode(mode === "return" ? null : "return")}>
              {t("act.return")}
            </button>
          </>
        )}
        {(it.kind === "ask" || it.kind === "mention") && (
          <button className="btn-accent" disabled={busy} onClick={() => setMode(mode === "reply" ? null : "reply")}>
            <MessageSquare size={14} /> {t("act.reply")}
          </button>
        )}
        {it.kind === "mention" && (
          <button className="btn" disabled={busy} onClick={() => run(() => chatApi.read(it.channel_id!, it.id), t("needs.done.read"))}>
            {t("act.mark_read")}
          </button>
        )}
        {it.kind === "ask" && (
          <button className="btn" disabled={busy} onClick={() => run(() => tasksApi.complete(it.ref!), t("needs.done.closed"))}>
            {t("act.done")}
          </button>
        )}
        <Link to={it.link} className="btn">
          <ExternalLink size={13} /> {t("act.open")}
        </Link>
      </div>
      {mode && (
        <form
          className="flex flex-col gap-2 pl-[30px] sm:flex-row"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <label className="sr-only" htmlFor={`needs-${it.key}`}>
            {t(`needs.input.${mode}`)}
          </label>
          <input
            id={`needs-${it.key}`}
            autoFocus
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t(`needs.input.${mode}`)}
            className="h-9 min-w-0 flex-1 rounded border border-line bg-bg px-2.5 text-sm outline-none focus:border-accent"
          />
          <span className="flex gap-2">
            <button className="btn-accent h-9!" disabled={busy || (mode === "reply" && !text.trim())}>
              {mode === "reply" ? t("act.send") : mode === "reject" ? t("act.reject") : t("act.return")}
            </button>
            <button type="button" className="btn h-9!" onClick={() => setMode(null)}>
              {t("act.cancel")}
            </button>
          </span>
        </form>
      )}
    </li>
  );
}

/** "Čeká na tebe": everything that needs the owner, with the action right on the row. */
export default function NeedsInbox() {
  const needs = useNeedsMe();
  const c = needs?.counts;
  return (
    <Panel
      title={t("needs.title")}
      right={
        needs ? (
          <span className="text-xs text-ink-2">
            {needs.count === 0
              ? t("needs.none_short")
              : [
                  c!.approval && `${c!.approval} ${plural(c!.approval, t("needs.count.approval.one"), t("needs.count.approval.few"), t("needs.count.approval.many"))}`,
                  c!.ask && `${c!.ask} ${plural(c!.ask, t("needs.count.ask.one"), t("needs.count.ask.few"), t("needs.count.ask.many"))}`,
                  c!.review && `${c!.review} ${plural(c!.review, t("needs.count.review.one"), t("needs.count.review.few"), t("needs.count.review.many"))}`,
                  c!.mention && `${c!.mention} ${plural(c!.mention, t("needs.count.mention.one"), t("needs.count.mention.few"), t("needs.count.mention.many"))}`,
                ]
                  .filter(Boolean)
                  .join(" · ")}
          </span>
        ) : (
          t("act.loading")
        )
      }
      className="border-amber-400/30"
    >
      {needs?.count === 0 && <p className="px-4 py-5 text-sm text-ink-2">{t("needs.none")}</p>}
      <ul className="max-h-[460px] overflow-y-auto">
        {needs?.items.map((it) => <Item key={it.key} it={it} />)}
      </ul>
    </Panel>
  );
}
