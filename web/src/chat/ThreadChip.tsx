// The thread chip under a channel message (who answered, how many, the last reply). Its own module so
// the chat list (/m's start screen, chat/ThreadList) does not pull the whole Messenger into the entry.
import { ChevronRight } from "lucide-react";
import type { ThreadSummary } from "../chatApi";
import { LOCALE, t } from "../i18n/core";
import { Avatar } from "../mobile/ui";
import { ago } from "./timeline";

const agoWords = () => ({
  today: t("m.chat.today"), yesterday: t("m.chat.yesterday"), locale: LOCALE,
  now: t("m.chat.ago_now"), min: t("m.chat.ago_min", { n: "{n}" }), hours: t("m.chat.ago_h", { n: "{n}" }),
});
export const replyCount = (n: number) => t(n === 1 ? "chat.reply_one" : n > 1 && n < 5 ? "chat.reply_few" : "chat.reply_many", { n });

/** Under a message in a channel: who answered, how many, the last reply in one line, when, unread. */
export function ThreadChip({ count, thread, onOpen }: { count: number; thread?: ThreadSummary | null; onOpen: () => void }) {
  const last = thread?.last;
  const unread = thread?.unread ?? 0;
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
      aria-label={t("m.chat.open_thread_n", { n: count })}
      className={`mt-1 flex w-full max-w-[340px] min-w-0 items-center gap-2 rounded-xl border px-2 py-1.5 text-left transition active:bg-raised hover:bg-raised ${unread ? "border-accent/50 bg-accent/5" : "border-line bg-surface/60"}`}
    >
      <span className="flex shrink-0 -space-x-1.5">
        {(thread?.repliers ?? []).slice(0, 3).map((r) => (
          <span key={r.id} className="rounded-full ring-2 ring-bg">
            <Avatar name={r.name} size={22} human={r.kind === "human"} />
          </span>
        ))}
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex items-center gap-1.5 text-[13px]">
          <span className="font-medium text-accent">{replyCount(count)}</span>
          {last && <span className="truncate text-[12px] text-ink-2">· {ago(last.created_at, new Date(), agoWords())}</span>}
          {unread > 0 && <span className="ml-auto shrink-0 rounded-full bg-accent px-1.5 font-mono text-[11px] leading-[18px] text-bg">{unread}</span>}
        </span>
        {last && (
          <span className="truncate text-[12.5px] text-ink-2">
            <span className="text-ink">{last.author_name}:</span> {last.body}
          </span>
        )}
      </span>
      <ChevronRight size={16} className="shrink-0 text-ink-2" />
    </button>
  );
}
