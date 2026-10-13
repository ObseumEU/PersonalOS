import { AlertCircle, Check, ChevronDown, ChevronRight, Clock, CornerUpLeft, Loader2 } from "lucide-react";
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api";
import { type ChatMessage, type Priority, type ThreadSummary, type TypingEntry, chatApi } from "../chatApi";
import { FileCard } from "../files/FileCard";
import { LOCALE, t } from "../i18n/core";
import { Avatar } from "../mobile/ui";
import { ThreadChip, replyCount } from "./ThreadChip";
import { Rich } from "./Rich";
import { type Item, type Row, ago, channelTimeline, dmTimeline, rows as toRows, threadTimeline } from "./timeline";
import { type OutboxFile, type Pending, belongsTo, loadPending, reconcile, savePending, storage } from "./outbox";
import { type AnswerStatus, animated, answerLabel, inlineReplies, typingKey } from "./answers";

/*
 * The messenger view (docs/MOBILE.md): the owner's messages on the right in the accent colour,
 * everyone else's on the left with an avatar and a name at the start of a group; day
 * separators; typing as a bubble; the newest at the bottom, "↓ nové zprávy" when scrolled up,
 * older messages load at the top. Used by the phone app (/m) and by the desktop chat for DMs
 * and threads.
 */

export type FileAtt = OutboxFile;
export type { Pending };

/** Optimistic sending: the bubble appears immediately (sending → sent); a failure offers a retry.
 * The server's stored message is handed on only when it belongs to the conversation open now (by its
 * channel_id, never by the view: prod 2026-10-13 the owner's DM message landed in #obseum-ai), and what is
 * still on its way or failed is kept per conversation across reloads (chat/outbox). `messages` (with `me`)
 * lets a message restored after a reload drop out once it is found in the conversation. */
export function useSender(channelId: number | null, onSent: (m: ChatMessage) => void, messages?: ChatMessage[], me?: number) {
  const kv = useMemo(storage, []);
  const [pending, setPending] = useState<Pending[]>(() => (channelId ? loadPending(kv, channelId) : []));
  const sentRef = useRef(onSent);
  sentRef.current = onSent;
  const channelRef = useRef(channelId);
  channelRef.current = channelId;
  const touched = useRef(new Set<number>());
  // Another conversation: its outbox from earlier page loads joins (once).
  const loaded = useRef(new Set<number>());
  useEffect(() => {
    if (!channelId || loaded.current.has(channelId)) return;
    loaded.current.add(channelId);
    const back = loadPending(kv, channelId);
    if (back.length) setPending((ps) => [...ps.filter((p) => p.channel_id !== channelId), ...back]);
  }, [channelId, kv]);
  useEffect(() => {
    for (const p of pending) touched.current.add(p.channel_id);
    savePending(kv, pending, touched.current);
  }, [pending, kv]);
  useEffect(() => {
    if (!messages || !me) return;
    setPending((ps) => {
      const next = reconcile(ps, messages, me);
      return next.length === ps.length ? ps : next;
    });
  }, [messages, me]);
  const post = useCallback((p: Pending) => {
    api<ChatMessage>(`/api/chat/channels/${p.channel_id}/messages`, {
      method: "POST",
      body: JSON.stringify({ body: p.body, reply_to: p.reply_to, priority: p.priority, attachments: p.files.map((f) => ({ type: "file", id: f.id })) }),
    }).then(
      (m) => {
        setPending((ps) => ps.filter((x) => x.key !== p.key));
        if (belongsTo(m, channelRef.current)) sentRef.current(m);
      },
      (e) => setPending((ps) => ps.map((x) => (x.key === p.key ? { ...x, state: "error", restored: false, error: e instanceof Error ? e.message : String(e) } : x))),
    );
  }, []);
  const send = useCallback(
    (body: string, files: FileAtt[] = [], reply_to: number | null = null, priority: Priority | null = null) => {
      if (!channelId || (!body.trim() && !files.length)) return;
      const p: Pending = {
        key: `p${Date.now()}${Math.random().toString(36).slice(2, 6)}`,
        channel_id: channelId, body: body.trim(), files, reply_to, priority, state: "sending", created_at: new Date().toISOString(),
      };
      setPending((ps) => [...ps, p]);
      post(p);
    },
    [channelId, post],
  );
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const retry = useCallback((key: string) => {
    // Not inside the state updater: React may run an updater twice (StrictMode), which sent twice.
    const p = pendingRef.current.find((x) => x.key === key);
    if (!p || p.state !== "error") return;
    setPending((ps) => ps.map((x) => (x.key === key ? { ...x, state: "sending", error: undefined, restored: false } : x)));
    post({ ...p, state: "sending" });
  }, [post]);
  const discard = useCallback((key: string) => setPending((ps) => ps.filter((x) => x.key !== key)), []);
  const here = useMemo(() => pending.filter((p) => p.channel_id === channelId), [pending, channelId]);
  return { pending: here, send, retry, discard };
}

/** The body without the "📎 name (soubor #12)" lines the server adds for agents (the previews show them). */
export const visibleBody = (m: ChatMessage) => (m.attachments.some((a) => a.type === "file") ? m.body.replace(/^📎 .+ \(soubor #\d+\)$/gm, "").trim() : m.body);

function Attachments({ files, mine }: { files: FileAtt[]; mine: boolean }) {
  if (!files.length) return null;
  return (
    <div className="mt-1 flex flex-col gap-1.5">
      {files.map((f) => (
        <FileCard key={`${f.id}-${f.version ?? ""}`} file={f} mine={mine} />
      ))}
    </div>
  );
}

const hhmm = (iso: string) => new Date(iso).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
export { ThreadChip, replyCount };

type BubbleProps = {
  row: Extract<Row, { kind: "msg" }>;
  names: string[];
  mode: "dm" | "channel" | "thread";
  onTap?: (m: ChatMessage) => void;
  onOpenThread?: (root: number) => void;
  onQuote?: (id: number) => void;
  actions?: (m: ChatMessage) => ReactNode;
  flash: boolean;
  /** Agents' replies to this (the owner's) message, shown under it in a channel. */
  inline?: ChatMessage[];
};

function corners(mine: boolean, first: boolean, last: boolean) {
  const side = mine ? ["rounded-tr-[6px]", "rounded-br-[6px]"] : ["rounded-tl-[6px]", "rounded-bl-[6px]"];
  return ["rounded-[18px]", first ? "" : side[0], last ? "" : side[1]].join(" ");
}

function Bubble({ row, names, mode, onTap, onOpenThread, onQuote, actions, flash, inline }: BubbleProps) {
  const { item, mine, first, last } = row;
  const m = item.m;
  const text = visibleBody(m);
  const files = m.attachments.filter((a) => a.type === "file") as unknown as FileAtt[];
  const showAvatar = !mine;
  return (
    <div id={`msg-${m.id}`} className={`group flex items-start gap-2 px-3 ${first ? "mt-2.5" : "mt-[3px]"} ${mine ? "justify-end" : ""}`}>
      {showAvatar &&
        (first ? (
          <span className="mt-[18px] shrink-0">
            <Avatar name={m.author_name} size={28} human={m.author_kind === "human"} />
          </span>
        ) : (
          <span className="w-7 shrink-0" />
        ))}
      <div className={`flex max-w-[82%] min-w-0 flex-col md:max-w-[70%] ${mine ? "items-end" : "items-start"}`}>
        {first && !mine && (
          <span className="mb-0.5 flex flex-wrap items-center gap-1.5 px-3 text-[12px]">
            <span className={m.author_kind === "human" ? "text-ink-2" : "font-medium text-accent"}>{m.author_name}</span>
            {m.meeting && <span className="rounded-[3px] border border-line px-1 text-[11px] text-ink-2">{m.meeting.kind === "decision" ? t("chat.meeting_decision") : t("chat.meeting")}</span>}
          </span>
        )}
        <div className={`flex max-w-full items-center gap-1 ${mine ? "flex-row-reverse" : ""}`}>
          <div
            role={onTap ? "button" : undefined}
            tabIndex={onTap ? 0 : undefined}
            onClick={onTap ? () => onTap(m) : undefined}
            onKeyDown={onTap ? (e) => e.key === "Enter" && e.target === e.currentTarget && onTap(m) : undefined}
            aria-label={onTap ? t("m.chat.actions") : undefined}
            className={`min-w-0 max-w-full px-3.5 py-2 text-left transition-shadow ${corners(mine, first, last)} ${
              mine ? "bg-accent text-bg" : "border border-line bg-raised text-ink"
            } ${m.meeting?.kind === "decision" ? "ring-1 ring-emerald-400/60" : ""} ${flash ? "ring-2 ring-amber-300" : ""}`}
          >
            {item.quote && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onQuote?.(item.quote!.id);
                }}
                aria-label={t("m.chat.reply_to", { name: item.quote.author_name })}
                className={`mb-1.5 flex w-full min-w-0 items-start gap-1.5 rounded-lg border-l-2 px-2 py-1 text-left text-[12.5px] leading-snug ${mine ? "border-bg/50 bg-black/10" : "border-accent/70 bg-bg/50"}`}
              >
                <CornerUpLeft size={12} className="mt-0.5 shrink-0 opacity-70" />
                <span className="min-w-0">
                  <span className="font-medium">{item.quote.author_name}</span>
                  <span className="line-clamp-2 opacity-80">{item.quote.body}</span>
                </span>
              </button>
            )}
            {m.priority && m.priority !== "fyi" && (
              <span className="mb-1 inline-block rounded-[4px] border border-current/40 px-1 text-[11px]">{t(`priority.${m.priority}`)}</span>
            )}
            {text && <Rich text={text} names={names} tone={mine ? "mine" : "theirs"} />}
            <Attachments files={files} mine={mine} />
            <span className={`mt-0.5 flex items-center justify-end gap-1 text-[11px] leading-none ${mine ? "text-bg/70" : "text-ink-2"}`}>
              {m.edited_at ? `${t("chat.edited")} · ` : ""}
              {hhmm(m.created_at)}
              {mine && <Check size={12} aria-label={t("m.chat.sent")} />}
            </span>
          </div>
          {actions && <span className="hidden shrink-0 group-hover:flex group-focus-within:flex">{actions(m)}</span>}
        </div>
        {m.reactions.length > 0 && (
          <span className={`-mt-1.5 flex flex-wrap gap-1 px-2 ${mine ? "justify-end" : ""}`}>
            {m.reactions.map((r) => (
              <span key={r.emoji} className="rounded-full border border-line bg-surface px-1.5 text-[12px] leading-5">
                {r.emoji}
                {r.count > 1 ? ` ${r.count}` : ""}
              </span>
            ))}
          </span>
        )}
        {mode === "channel" && inline && inline.length > 0 && <InlineReplies replies={inline} names={names} onOpen={onOpenThread ? () => onOpenThread(m.id) : undefined} />}
        {mode === "channel" && m.replies > 0 && onOpenThread && <ThreadChip count={m.replies} thread={m.thread} onOpen={() => onOpenThread(m.id)} />}
      </div>
    </div>
  );
}

function PendingBubble({ p, names, onRetry, onDiscard }: { p: Pending; names: string[]; onRetry: () => void; onDiscard: () => void }) {
  const failed = p.state === "error";
  return (
    <div className="mt-[3px] flex flex-col items-end px-3">
      <div className={`max-w-[82%] rounded-[18px] rounded-tr-[6px] bg-accent px-3.5 py-2 text-bg md:max-w-[70%] ${failed ? "opacity-60" : "opacity-85"}`}>
        {p.body && <Rich text={p.body} names={names} tone="mine" />}
        {p.files.length > 0 && <Attachments files={p.files} mine />}
        <span className="mt-0.5 flex items-center justify-end gap-1 text-[11px] leading-none text-bg/70">
          {hhmm(p.created_at)}
          {failed ? <AlertCircle size={12} /> : <Clock size={12} aria-label={t("m.chat.sending")} />}
        </span>
      </div>
      {failed && (
        <span className="mt-1 flex items-center gap-3 text-[12px]">
          <span className="text-red-400" title={p.error}>{t("m.chat.failed")}</span>
          <button type="button" onClick={onRetry} className="h-8 font-medium text-accent">{t("m.chat.retry")}</button>
          <button type="button" onClick={onDiscard} className="h-8 text-ink-2">{t("m.chat.discard")}</button>
        </span>
      )}
    </div>
  );
}

function TypingBubble({ entries }: { entries: TypingEntry[] }) {
  const first = entries[0];
  const soft = entries.every((e) => e.state !== "typing");
  const moving = entries.some(animated);
  const label = entries.map((e) => `${e.name} ${t(typingKey(e))}`).join(" · ");
  return (
    <div className="mt-2.5 flex items-end gap-2 px-3" role="status" aria-live="polite" aria-label={label}>
      <Avatar name={first.name} size={28} human={first.kind === "human"} />
      <div className="flex flex-col items-start">
        <span className="mb-0.5 px-3 text-[12px] text-ink-2">{label}</span>
        <div className="flex h-9 items-center rounded-[18px] rounded-bl-[6px] border border-line bg-raised px-4 text-ink-2">
          {moving ? (
            <span className={`typing-dots big ${soft ? "soft" : ""}`} aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
          ) : (
            <Clock size={15} aria-hidden="true" />
          )}
        </div>
      </div>
    </div>
  );
}

/** Under the owner's message: who took it and where the answer stands; "Připomenout" when nobody did. */
export function AnswerLine({ st, onOpenThread, className = "" }: { st: AnswerStatus; onOpenThread?: () => void; className?: string }) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const l = answerLabel(st, LOCALE);
  const remind = () => {
    setBusy(true);
    setFailed(null);
    chatApi.remind(st.message_id).then(
      () => setBusy(false),
      (e) => {
        setBusy(false);
        setFailed(e instanceof Error ? e.message : String(e));
      },
    );
  };
  const tone = st.state === "unclaimed" || st.state === "held" ? "text-amber-300" : "text-ink-2";
  return (
    <div className={`flex flex-wrap items-center justify-end gap-x-2 gap-y-1 px-3 pt-1 text-[12px] ${tone} ${className}`} role="status" aria-live="polite" data-answer-state={st.state}>
      {st.state === "working" && (
        <span className="typing-dots" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
      )}
      {(st.state === "queued" || st.state === "held") && <Clock size={12} aria-hidden="true" />}
      {st.state === "answered" && <Check size={12} aria-hidden="true" />}
      {st.state === "unclaimed" && <AlertCircle size={12} aria-hidden="true" />}
      <span>{t(l.key, l.params)}</span>
      {st.state === "unclaimed" && (
        <button type="button" onClick={remind} disabled={busy} className="h-7 rounded-full border border-accent/60 px-3 font-medium text-accent disabled:opacity-60">
          {busy ? <Loader2 size={12} className="animate-spin" /> : t("chat.answer.remind")}
        </button>
      )}
      {st.state === "answered" && onOpenThread && st.thread == null && (
        <button type="button" onClick={onOpenThread} className="h-7 font-medium text-accent">{t("chat.answer.show")}</button>
      )}
      {failed && <span className="text-red-400" title={failed}>{t("chat.answer.remind_failed")}</span>}
    </div>
  );
}

/** Agents' replies to the owner's message, under it in the channel (the thread stays one tap away). */
export function InlineReplies({ replies, names, onOpen }: { replies: ChatMessage[]; names: string[]; onOpen?: () => void }) {
  if (!replies.length) return null;
  return (
    <div className="mt-1 flex w-full max-w-[520px] flex-col gap-1">
      {replies.map((r) => (
        <div key={r.id} className="rounded-xl border border-accent/30 bg-accent/5 px-3 py-1.5 text-left" data-inline-reply={r.id}>
          <span className="flex items-center gap-1.5 text-[12px]">
            <CornerUpLeft size={12} className="rotate-180 text-accent" aria-hidden="true" />
            <span className="font-medium text-accent">{r.author_name}</span>
            <span className="text-ink-2">{hhmm(r.created_at)}</span>
            {onOpen && (
              <button type="button" onClick={onOpen} className="ml-auto text-ink-2 hover:text-accent">{t("chat.answer.in_thread")}</button>
            )}
          </span>
          <Rich text={visibleBody(r)} names={names} tone="theirs" />
        </div>
      ))}
    </div>
  );
}

/** A thread's root, pinned at the top as the context. */
function RootCard({ m, names, channel }: { m: ChatMessage; names: string[]; channel?: string }) {
  const [open, setOpen] = useState(false);
  const text = visibleBody(m);
  return (
    <div className="sticky top-0 z-10 border-b border-line bg-bg/95 px-3 pt-2 pb-2.5 backdrop-blur">
      <div className="flex items-center gap-2 text-[12px] text-ink-2">
        <Avatar name={m.author_name} size={20} human={m.author_kind === "human"} />
        <span className="font-medium text-ink">{m.author_name}</span>
        {channel && <span>· #{channel}</span>}
        <span className="ml-auto">{hhmm(m.created_at)}</span>
      </div>
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} className="mt-1 block w-full text-left">
        <div className={open ? "max-h-[45dvh] overflow-y-auto" : "max-h-[4.6rem] overflow-hidden [mask-image:linear-gradient(to_bottom,black_60%,transparent)]"}>
          <Rich text={text} names={names} collapsible={false} />
        </div>
      </button>
      <Attachments files={m.attachments.filter((a) => a.type === "file") as unknown as FileAtt[]} mine={false} />
    </div>
  );
}

export type MessengerProps = {
  mode: "dm" | "channel" | "thread";
  /** What the view shows; a new key starts at the bottom again (another conversation or thread). */
  viewKey: string;
  messages: ChatMessage[];
  root?: ChatMessage | null;
  rootChannel?: string;
  me: number;
  names: string[];
  pending?: Pending[];
  typing?: TypingEntry[];
  /** Where the answer to the owner's latest message here stands (chat/answers). */
  answer?: AnswerStatus | null;
  hasMore?: boolean;
  onOlder?: () => Promise<unknown>;
  onTap?: (m: ChatMessage) => void;
  onOpenThread?: (root: number) => void;
  onRetry?: (key: string) => void;
  onDiscard?: (key: string) => void;
  actions?: (m: ChatMessage) => ReactNode;
  empty?: ReactNode;
  top?: ReactNode;
};

export default function Messenger(props: MessengerProps) {
  const { mode, viewKey, messages, root, me, names, pending = [], typing = [], hasMore, onOlder } = props;
  const box = useRef<HTMLDivElement>(null);
  const inner = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const anchor = useRef<{ h: number; top: number } | null>(null);
  const loading = useRef(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  // Older messages failed to load: no new try on every scroll, a "Zkusit znovu" button instead.
  const [olderFailed, setOlderFailed] = useState(false);
  const olderFailedRef = useRef(false);
  useEffect(() => {
    olderFailedRef.current = false;
    setOlderFailed(false);
  }, [viewKey]);
  const [unseen, setUnseen] = useState(0);
  const [flash, setFlash] = useState<number | null>(null);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const h = setInterval(() => setNow(new Date()), 60000);
    return () => clearInterval(h);
  }, []);

  const items: Item[] = useMemo(
    () => (mode === "dm" ? dmTimeline(messages) : mode === "thread" && root ? threadTimeline(messages, root.id) : channelTimeline(messages)),
    [mode, messages, root],
  );
  const list = useMemo(() => toRows(items, me, now, { today: t("m.chat.today"), yesterday: t("m.chat.yesterday"), locale: LOCALE }), [items, me, now]);

  const toBottom = useCallback((smooth = false) => {
    const el = box.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" });
    atBottom.current = true;
    setUnseen(0);
  }, []);

  // Another conversation: start at the newest.
  useLayoutEffect(() => {
    atBottom.current = true;
    setUnseen(0);
    toBottom();
  }, [viewKey, toBottom]);

  const firstId = items[0]?.m.id ?? 0;
  const lastMsg = items[items.length - 1]?.m;
  const tailKey = `${lastMsg?.id ?? 0}:${pending.length}:${typing.length}:${props.answer?.state ?? ""}:${messages.length}`;
  const prevFirst = useRef(firstId);
  const prevTail = useRef(tailKey);
  const prevCount = useRef(items.length);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    if (anchor.current && firstId !== prevFirst.current) {
      // Older messages came in above: keep what was on screen where it was.
      el.scrollTop = el.scrollHeight - anchor.current.h + anchor.current.top;
      anchor.current = null;
    } else if (tailKey !== prevTail.current) {
      const added = Math.max(0, items.length - prevCount.current);
      const mineNew = (lastMsg && lastMsg.author_id === me && added > 0) || pending.length > 0;
      if (atBottom.current || mineNew) toBottom(true);
      else if (added > 0) setUnseen((n) => n + added);
    }
    prevFirst.current = firstId;
    prevTail.current = tailKey;
    prevCount.current = items.length;
  }, [firstId, tailKey, items.length, lastMsg, me, pending.length, toBottom]);

  // The keyboard opening (the viewport shrinks) or an image loading keeps the newest in view.
  useEffect(() => {
    const el = box.current;
    const content = inner.current;
    if (!el || !content || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (atBottom.current && !anchor.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(el);
    ro.observe(content);
    return () => ro.disconnect();
  }, []);

  const older = useCallback((retry = false) => {
    const el = box.current;
    if (!onOlder || !hasMore || loading.current || !el || (olderFailedRef.current && !retry)) return;
    loading.current = true;
    setLoadingOlder(true);
    anchor.current = { h: el.scrollHeight, top: el.scrollTop };
    const failed = (v: boolean) => {
      olderFailedRef.current = v;
      setOlderFailed(v);
    };
    onOlder().then(() => failed(false), () => failed(true)).finally(() => {
      loading.current = false;
      setLoadingOlder(false);
      // Nothing came (an error): forget the anchor.
      requestAnimationFrame(() => {
        anchor.current = null;
      });
    });
  }, [onOlder, hasMore]);

  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    atBottom.current = bottom;
    if (bottom && unseen) setUnseen(0);
    if (el.scrollTop < 160) older();
  };

  const jump = (id: number) => {
    const target = document.getElementById(`msg-${id}`);
    if (!target) return;
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    setFlash(id);
    setTimeout(() => setFlash((f) => (f === id ? null : f)), 1400);
  };

  const typingEntries = typing.filter((e) => e.id !== me);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div ref={box} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto overscroll-contain" style={{ overflowAnchor: "none" }}>
        {mode === "thread" && root && <RootCard m={root} names={names} channel={props.rootChannel} />}
        <div ref={inner} className="pt-1 pb-3">
          {props.top}
          {hasMore && (
            <div className="flex h-10 items-center justify-center text-[12px] text-ink-2">
              {loadingOlder ? (
                <span className="flex items-center gap-2"><Loader2 size={14} className="animate-spin" /> {t("m.chat.loading_older")}</span>
              ) : olderFailed ? (
                <span role="alert" className="flex items-center gap-1">
                  <AlertCircle size={14} className="text-red-400" /> {t("m.chat.older_failed")}
                  <button type="button" onClick={() => older(true)} className="h-10 px-2 text-accent">{t("m.error.retry")}</button>
                </span>
              ) : (
                <button type="button" onClick={() => older(true)} className="h-10 px-4">{t("m.chat.load_older")}</button>
              )}
            </div>
          )}
          {list.length === 0 && pending.length === 0 && props.empty}
          {list.map((r) =>
            r.kind === "day" ? (
              <div key={r.key} className="my-3 flex justify-center">
                <span className="rounded-full border border-line bg-surface px-3 py-0.5 text-[12px] text-ink-2">{r.label}</span>
              </div>
            ) : (
              <Fragment key={r.key}>
                <Bubble
                  row={r}
                  names={names}
                  mode={mode}
                  onTap={props.onTap}
                  onOpenThread={props.onOpenThread}
                  onQuote={jump}
                  actions={props.actions}
                  flash={flash === r.item.m.id}
                  inline={mode === "channel" ? inlineReplies(messages, r.item.m, me) : undefined}
                />
                {props.answer && props.answer.message_id === r.item.m.id && (
                  <AnswerLine st={props.answer} onOpenThread={mode === "channel" && props.onOpenThread ? () => props.onOpenThread!(r.item.m.id) : undefined} />
                )}
              </Fragment>
            ),
          )}
          {pending.map((p) => (
            <PendingBubble key={p.key} p={p} names={names} onRetry={() => props.onRetry?.(p.key)} onDiscard={() => props.onDiscard?.(p.key)} />
          ))}
          {typingEntries.length > 0 && <TypingBubble entries={typingEntries} />}
        </div>
      </div>
      {unseen > 0 && (
        <button
          type="button"
          onClick={() => toBottom(true)}
          className="absolute bottom-3 left-1/2 z-10 flex h-9 -translate-x-1/2 items-center gap-1.5 rounded-full border border-accent/60 bg-surface px-4 text-[13px] font-medium text-accent shadow-lg"
        >
          <ChevronDown size={16} /> {unseen > 1 ? `${unseen} ` : ""}{t("m.chat.new_messages")}
        </button>
      )}
    </div>
  );
}
