import { ChevronDown } from "lucide-react";
import { type ReactNode, useState } from "react";
import { t } from "../i18n";
import Markdown from "./Markdown";

/** An approval as the owner reads it (backend pos.approval_view): never the agent's raw JSON. */
export type ApprovalView = {
  title: string;
  action_label: string;
  kind: string;
  kind_label: string;
  reason: string;
  why: string;
  notes: string;
  approve_label: string;
  approve_hint: string;
  mode?: "draft" | "auto";
  email?: { to: string; cc: string; subject: string; body: string; account: string };
  text?: string;
  commands?: string[];
};

/** The approve button's words: what really happens ("Uložit jako koncept v Gmailu", "Odeslat"). */
export function approveLabel(v: ApprovalView | null | undefined): string {
  return v?.approve_label || t("act.approve");
}

/** A long text folded to a few lines, with "Zobrazit celé". */
function Folded({ text, lines = 6, markdown = false }: { text: string; lines?: number; markdown?: boolean }) {
  const [open, setOpen] = useState(false);
  const long = text.split("\n").length > lines || text.length > lines * 90;
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className={`min-w-0 text-[13px] leading-relaxed break-words whitespace-pre-wrap ${long && !open ? "line-clamp-6" : ""}`}>
        {markdown ? <Markdown text={text} compact className="text-[13px]" /> : text}
      </div>
      {long && (
        <button type="button" className="inline-flex items-center gap-1 self-start text-xs text-accent hover:underline" aria-expanded={open} onClick={() => setOpen(!open)}>
          <ChevronDown size={12} className={open ? "rotate-180" : ""} /> {open ? t("appr.text_less") : t("appr.text_more")}
        </button>
      )}
    </div>
  );
}

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="contents">
      <dt className="text-ink-2">{k}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

/** Kind, reason, why, then the thing itself: an e-mail as Komu / Předmět / Text, a post's text, commands. */
export default function ApprovalBody({ v, compact = false }: { v: ApprovalView; compact?: boolean }) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      {(v.kind_label || v.reason) && (
        <p className="text-[12px] text-amber-200/90">
          {v.kind_label}
          {v.kind_label && v.reason ? " · " : ""}
          <span className="text-ink-2">{v.reason}</span>
        </p>
      )}
      {v.why && <p className={`text-[13px] break-words text-ink-2 ${compact ? "line-clamp-2" : ""}`}>{v.why}</p>}
      {v.email && (
        <dl className="grid grid-cols-[70px_minmax(0,1fr)] gap-x-3 gap-y-1 rounded-md border border-line bg-bg p-3 text-[13px]">
          <Row k={t("appr.email.to")}>{v.email.to || "—"}</Row>
          {v.email.cc && <Row k={t("appr.email.cc")}>{v.email.cc}</Row>}
          <Row k={t("appr.email.subject")}>{v.email.subject || "—"}</Row>
          {v.email.account && <Row k={t("appr.email.from")}>{v.email.account}</Row>}
          <Row k={t("appr.email.body")}>
            <Folded text={v.email.body} lines={compact ? 4 : 10} />
          </Row>
        </dl>
      )}
      {v.text && (
        <div className="rounded-md border border-line bg-bg p-3">
          <Folded text={v.text} lines={compact ? 4 : 12} />
        </div>
      )}
      {v.commands && v.commands.length > 0 && (
        <pre className="overflow-x-auto rounded-md border border-line bg-bg p-2 font-mono text-[12px] whitespace-pre-wrap">{v.commands.join("\n")}</pre>
      )}
      {v.notes && <p className="text-[13px] break-words text-ink-2 italic">{v.notes}</p>}
      {v.approve_hint && <p className="text-[12px] text-ink-3">{v.approve_hint}</p>}
    </div>
  );
}
