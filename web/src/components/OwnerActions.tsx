import { Check, ExternalLink, KeyRound, Mail, Send, Trash2, X } from "lucide-react";
import { useState } from "react";
import { t } from "../i18n/core";
import { type NeedsItem, type WaitingDraft, ownerActions, refreshNeedsMe } from "../needsMeApi";
import { toast } from "./overlay";

/** Icons of the owner's one-click kinds in "Čeká na tebe" (the web Home and /m share them). */
export const OWNER_KINDS = {
  access: { Icon: KeyRound, key: "needs.kind.access", cls: "text-amber-300" },
  publish: { Icon: Send, key: "needs.kind.publish", cls: "text-accent" },
  draft: { Icon: Mail, key: "needs.kind.draft", cls: "text-accent" },
} as const;

type Cls = { primary: string; plain: string; size: number };

function DraftButtons({ d, cls, busy, mark }: { d: WaitingDraft; cls: Cls; busy: boolean; mark: (s: "sent" | "discarded") => void }) {
  return (
    <span className="flex flex-wrap gap-2">
      {d.link && (
        <a href={d.link} target="_blank" rel="noreferrer noopener" className={cls.plain}>
          <ExternalLink size={cls.size - 1} /> {t("needs.act.open_gmail")}
        </a>
      )}
      <button className={cls.primary} disabled={busy} onClick={() => mark("sent")}>
        <Check size={cls.size} /> {t("needs.act.draft_sent")}
      </button>
      <button className={cls.plain} disabled={busy} onClick={() => mark("discarded")}>
        <Trash2 size={cls.size} /> {t("needs.act.draft_discard")}
      </button>
    </span>
  );
}

/** One draft of a campaign: who it is for, Gmail, and "Odesláno" / "Zahodit" (the ask stays until all are done). */
function DraftRow({ d, cls }: { d: WaitingDraft; cls: Cls }) {
  const [busy, setBusy] = useState(false);
  const [gone, setGone] = useState(false);
  if (gone) return null;
  const mark = async (state: "sent" | "discarded") => {
    setBusy(true);
    try {
      await ownerActions.markDraft(d.mark_url, state);
      setGone(true);
      toast(t(state === "sent" ? "needs.done.draft_sent" : "needs.done.draft_discarded"));
      refreshNeedsMe();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="flex flex-col gap-1.5 border-t border-line pt-2 first:border-0 first:pt-0">
      <span className="text-[13px] break-words">
        <span className="font-medium">{d.to ?? "?"}</span>
        {d.subject ? ` · ${d.subject}` : ""}
        {d.agent ? <span className="text-ink-2"> · {d.agent}</span> : null}
      </span>
      <DraftButtons d={d} cls={cls} busy={busy} mark={mark} />
    </li>
  );
}

/** The buttons of an access request, an approved LinkedIn post, a waiting draft, and the drafts listed on
 * a draft campaign's ask. `run` acts on the whole item (it leaves the list). Small: ships in /m too. */
export default function OwnerActions({ it, busy, run, cls }: { it: NeedsItem; busy: boolean; run: (p: () => Promise<unknown>, done: string) => void; cls: Cls }) {
  if (it.kind === "access" && it.decide_url) {
    const url = it.decide_url;
    return (
      <>
        <button className={cls.primary} disabled={busy} onClick={() => run(() => ownerActions.decideAccess(url, true), t("needs.done.granted"))}>
          <Check size={cls.size} /> {t("needs.act.grant")}
        </button>
        <button className={cls.plain} disabled={busy} onClick={() => run(() => ownerActions.decideAccess(url, false), t("needs.done.denied"))}>
          <X size={cls.size} /> {t("needs.act.deny")}
        </button>
      </>
    );
  }
  if (it.kind === "publish" && it.publish_url) {
    const url = it.publish_url;
    return it.connected ? (
      <button className={cls.primary} disabled={busy} onClick={() => run(() => ownerActions.publish(url), t("needs.done.published"))}>
        <Send size={cls.size} /> {t("needs.act.publish")}
      </button>
    ) : (
      <>
        <a href={it.connect_url ?? "/api/integrations/linkedin/start"} className={cls.primary}>
          {t("needs.act.connect_linkedin")}
        </a>
        <span className="w-full text-xs text-ink-2">{t("needs.publish.not_connected")}</span>
      </>
    );
  }
  if (it.kind === "draft" && it.draft) {
    const d = it.draft;
    // The Gmail link comes in it.links (the list shows it); here only the two marks.
    return (
      <DraftButtons
        d={{ ...d, link: null }}
        cls={cls}
        busy={busy}
        mark={(s) => run(() => ownerActions.markDraft(d.mark_url, s), t(s === "sent" ? "needs.done.draft_sent" : "needs.done.draft_discarded"))}
      />
    );
  }
  if (it.drafts?.length) {
    return (
      <ul className="flex w-full flex-col gap-2 rounded-lg border border-line p-2.5">
        {it.drafts.map((d) => (
          <DraftRow key={d.id} d={d} cls={cls} />
        ))}
      </ul>
    );
  }
  return null;
}
