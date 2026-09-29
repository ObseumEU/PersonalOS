import { Hash } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { type ThreadItem, chatApi } from "../chatApi";
import { t } from "../i18n/core";
import { Avatar } from "../mobile/ui";
import { stripToolMarkup } from "../toolMarkup";
import { ThreadChip } from "./Messenger";

/** Threads in the owner's channels, the latest activity first, with unread replies ("Vlákna"). */
export function useThreads(listen: (reload: () => void) => () => void) {
  const [data, setData] = useState<{ threads: ThreadItem[]; unread: number } | null>(null);
  const load = useCallback(() => chatApi.threads().then(setData, () => undefined), []);
  useEffect(() => {
    load();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const off = listen(() => {
      clearTimeout(timer);
      timer = setTimeout(load, 400);
    });
    window.addEventListener("pos:resume", load);
    window.addEventListener("pos:threads", load);
    return () => {
      off();
      clearTimeout(timer);
      window.removeEventListener("pos:resume", load);
      window.removeEventListener("pos:threads", load);
    };
  }, [load, listen]);
  return data;
}

export default function ThreadList({ data, onOpen, active }: { data: { threads: ThreadItem[] } | null; onOpen: (it: ThreadItem) => void; active?: number | null }) {
  if (data === null) return <p className="px-4 py-6 text-sm text-ink-2">{t("act.loading")}</p>;
  if (!data.threads.length) return <p className="px-4 py-8 text-center text-sm text-ink-2">{t("m.chat.threads_empty")}</p>;
  return (
    <div className="flex flex-col">
      {data.threads.map((it) => {
        const unread = it.thread?.unread ?? 0;
        return (
          <div
            key={it.root.id}
            className={`flex flex-col gap-1 border-b border-line/60 px-4 py-3 ${active === it.root.id ? "bg-raised" : ""}`}
          >
            <button type="button" onClick={() => onOpen(it)} className="flex min-w-0 items-start gap-2.5 text-left">
              <Avatar name={it.root.author_name} size={32} human={it.root.author_kind === "human"} />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="flex items-center gap-1.5 text-[12px] text-ink-2">
                  <Hash size={12} />
                  <span className="truncate">{it.channel_name}</span>
                  <span>·</span>
                  <span className={`truncate ${unread ? "font-medium text-ink" : ""}`}>{it.root.author_name}</span>
                </span>
                <span className={`line-clamp-2 text-[14px] leading-snug ${unread ? "text-ink" : "text-ink-2"}`}>{stripToolMarkup(it.root.body)}</span>
              </span>
            </button>
            <span className="pl-[42px]">
              <ThreadChip count={it.replies} thread={it.thread} onOpen={() => onOpen(it)} />
            </span>
          </div>
        );
      })}
    </div>
  );
}
