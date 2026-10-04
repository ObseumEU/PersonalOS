import { AtSign, Check, CheckCheck, HelpCircle, MessageSquare, ShieldCheck, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { agentsApi } from "../agentsApi";
import { chatApi } from "../chatApi";
import { toast } from "../components/overlay";
import { ago, label, t } from "../i18n/core";
import DecisionOptions from "../components/DecisionOptions";
import { type NeedsItem, chooseOption, dropNeedsItem, refreshNeedsMe, useNeedsMe } from "../needsMeApi";
import { tasksApi } from "../tasksApi";
import { TopBar } from "./ui";

const KIND = {
  approval: { Icon: ShieldCheck, key: "needs.kind.approval", cls: "text-amber-300" },
  ask: { Icon: HelpCircle, key: "needs.kind.ask", cls: "text-amber-300" },
  review: { Icon: CheckCheck, key: "needs.kind.review", cls: "text-accent" },
  mention: { Icon: AtSign, key: "needs.kind.mention", cls: "text-accent" },
} as const;

type Mode = null | "reply" | "reject" | "return";

/** Where tapping the item goes inside the app: the task panel, or the conversation. */
function openHref(it: NeedsItem): string {
  if (it.kind === "mention" && it.channel_id) return `/m/chat/${it.channel_id}${it.thread && it.thread !== it.id ? `?thread=${it.thread}` : ""}`;
  if (it.ref) return `/m/needs?task=${it.ref}&focus=needs`;
  if (it.kind === "approval") return `/m/needs?approval=${it.id}`;
  return it.link;
}

function Item({ it, highlight }: { it: NeedsItem; highlight: boolean }) {
  const [mode, setMode] = useState<Mode>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLLIElement>(null);
  const { Icon, key, cls } = KIND[it.kind];
  useEffect(() => {
    if (highlight) ref.current?.scrollIntoView({ block: "center" });
  }, [highlight]);

  const run = async (p: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try {
      await p();
      dropNeedsItem(it.key);
      toast(done);
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
  const big = "flex h-11 items-center justify-center gap-1.5 rounded-lg border px-4 text-[15px] disabled:opacity-40";
  const primary = `${big} border-accent bg-accent/10 font-medium text-accent`;
  const plain = `${big} border-line text-ink-2`;

  return (
    <li ref={ref} className={`flex flex-col gap-2.5 border-b border-line px-4 py-3.5 ${highlight ? "bg-accent/5 shadow-[inset_3px_0_0_var(--color-accent)]" : ""}`}>
      <div className="flex min-w-0 items-start gap-3">
        <Icon size={20} strokeWidth={1.6} className={`mt-0.5 shrink-0 ${cls}`} aria-hidden />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex flex-wrap items-baseline gap-x-2 text-[12px]">
            <span className={`font-medium ${cls}`}>{t(key)}</span>
            {it.ref && <span className="font-mono text-ink-2">{it.ref}</span>}
            {it.blocking && <span className="text-amber-300">{t("needs.blocking")}</span>}
            <span className="text-ink-2">
              {it.from_name ? `${it.from_name} · ` : ""}
              {ago(it.at)}
            </span>
          </span>
          <Link to={openHref(it)} className="text-[15px] leading-snug break-words">
            {it.kind === "approval" && it.action ? label("approval", it.action) : it.title}
          </Link>
          {it.detail && <p className="line-clamp-3 text-[13px] break-words text-ink-2">{it.detail}</p>}
        </div>
      </div>
      <div className="flex flex-wrap gap-2 pl-8">
        {it.kind === "approval" && (
          <>
            <button className={primary} disabled={busy} onClick={() => run(() => agentsApi.decide(it.id, true), t("needs.done.approved"))}>
              <Check size={16} /> {t("act.approve")}
            </button>
            <button className={plain} disabled={busy} onClick={() => setMode(mode === "reject" ? null : "reject")}>
              <X size={16} /> {t("act.reject")}
            </button>
          </>
        )}
        {it.kind === "review" && (
          <>
            <button className={primary} disabled={busy} onClick={() => run(() => tasksApi.review(it.ref!, true), t("needs.done.accepted"))}>
              <Check size={16} /> {t("act.approve")}
            </button>
            <button className={plain} disabled={busy} onClick={() => setMode(mode === "return" ? null : "return")}>
              {t("act.return")}
            </button>
          </>
        )}
        {it.kind === "ask" && it.options?.length ? (
          <div className="w-full">
            <DecisionOptions it={it} big busy={busy} onChoose={(o) => run(() => chooseOption(it.ref!, o), t("needs.done.decided"))} />
          </div>
        ) : null}
        {(it.kind === "ask" || it.kind === "mention") && (
          <button className={primary} disabled={busy} onClick={() => setMode(mode === "reply" ? null : "reply")}>
            <MessageSquare size={16} /> {t("m.needs.reply")}
          </button>
        )}
        {it.kind === "mention" && (
          <button className={plain} disabled={busy} onClick={() => run(() => chatApi.read(it.channel_id!, it.id), t("needs.done.read"))}>
            {t("act.mark_read")}
          </button>
        )}
        {it.kind === "ask" && (
          <button className={plain} disabled={busy} onClick={() => run(() => tasksApi.complete(it.ref!), t("needs.done.closed"))}>
            {t("act.done")}
          </button>
        )}
      </div>
      {mode && (
        <form
          className="flex flex-col gap-2 pl-8"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <textarea
            autoFocus
            rows={2}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t(`needs.input.${mode}`)}
            aria-label={t(`needs.input.${mode}`)}
            className="min-h-20 rounded-lg border border-line bg-bg px-3 py-2 text-[16px] outline-none focus:border-accent"
          />
          <span className="flex gap-2">
            <button className={primary} disabled={busy || (mode === "reply" && !text.trim())}>
              {mode === "reply" ? t("act.send") : mode === "reject" ? t("act.reject") : t("act.return")}
            </button>
            <button type="button" className={plain} onClick={() => setMode(null)}>
              {t("act.cancel")}
            </button>
          </span>
        </form>
      )}
    </li>
  );
}

export default function Needs() {
  const needs = useNeedsMe();
  const [params] = useSearchParams();
  const focus = params.get("item");
  return (
    <div className="flex flex-col">
      <TopBar title={t("m.needs.title")} sub={needs && needs.count > 0 ? String(needs.count) : undefined} />
      {!needs && <p className="px-4 py-6 text-sm text-ink-2">{t("act.loading")}</p>}
      {needs?.count === 0 && <p className="px-6 py-10 text-center text-[15px] leading-relaxed text-ink-2">{t("m.needs.empty")}</p>}
      <ul>
        {needs?.items.map((it) => <Item key={it.key} it={it} highlight={it.key === focus} />)}
      </ul>
    </div>
  );
}
