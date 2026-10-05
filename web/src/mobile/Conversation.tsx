import { Camera, CheckCircle2, Copy, FileText, ListPlus, MessageSquare, Paperclip, Send, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { agentsApi } from "../agentsApi";
import { api } from "../api";
import { type Channel, type ChatMessage, chatApi } from "../chatApi";
import Messenger, { type FileAtt, useSender, visibleBody } from "../chat/Messenger";
import { applyReply, upsert } from "../chat/timeline";
import { toast } from "../components/overlay";
import { AttachChips, MicButton, useAttachments } from "../components/compose";
import { t } from "../i18n/core";
import { type Task } from "../tasksApi";
import { useMembers, workLabel } from "./ChatList";
import { isMessageEvent, onChatEvent, usePresence } from "./live";
import { ActionSheet, Avatar, SheetButton, TopBar } from "./ui";

const APPROVAL_REF = /schválení #(\d+)/i;

/** The composer, pinned to the bottom (above the keyboard, safe-area aware). Sending is optimistic: the parent shows the bubble at once. */
function Composer({ channel, replyTo, placeholder, onSend }: { channel: Channel; replyTo: number | null; placeholder: string; onSend: (body: string, files: FileAtt[]) => void }) {
  const [body, setBody] = useState("");
  const att = useAttachments();
  const { files, uploading } = att;
  const [picker, setPicker] = useState(false);
  const photo = useRef<HTMLInputElement>(null);
  const any = useRef<HTMLInputElement>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  const lastTyping = useRef(0);
  const desktop = useMemo(() => window.matchMedia("(pointer: fine)").matches, []);

  const grow = () => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  };
  const change = (v: string) => {
    setBody(v);
    grow();
    if (Date.now() - lastTyping.current > 3000) {
      lastTyping.current = Date.now();
      chatApi.typing(channel.id, replyTo).catch(() => undefined);
    }
  };
  const upload = (list: FileList | null) => {
    setPicker(false);
    void att.add(list);
  };
  // The dictated words go in like typed ones (the box grows with them).
  const dictate = (v: string) => {
    setBody(v);
    requestAnimationFrame(grow);
  };
  const submit = () => {
    const text = body.trim();
    if ((!text && !files.length) || uploading) return;
    onSend(text, files);
    setBody("");
    att.clear();
    requestAnimationFrame(grow);
    area.current?.focus(); // the keyboard stays open, like a messenger
  };
  const key = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (desktop && e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="shrink-0 border-t border-line bg-bg px-2 pt-2 pb-[max(8px,env(safe-area-inset-bottom))]">
      <AttachChips files={files} uploading={uploading} onRemove={att.remove} className="px-1 pb-2" />
      <div className="flex items-end gap-1.5">
        <button aria-label={t("m.chat.attach")} onClick={() => setPicker(true)} className="grid h-11 w-11 shrink-0 place-items-center rounded-full text-ink-2 active:bg-raised">
          <Paperclip size={20} />
        </button>
        <textarea
          ref={area}
          rows={1}
          value={body}
          onChange={(e) => change(e.target.value)}
          onPaste={att.onPaste}
          onKeyDown={key}
          placeholder={placeholder}
          aria-label={t("chat.message")}
          enterKeyHint={desktop ? "send" : "enter"}
          className="max-h-[140px] min-h-11 min-w-0 flex-1 resize-none rounded-3xl border border-line bg-surface px-4 py-2.5 text-[16px] leading-snug outline-none focus:border-accent"
        />
        <MicButton value={body} onChange={dictate} className="h-11 w-11" size={20} />
        <button
          aria-label={t("m.chat.send")}
          onPointerDown={(e) => e.preventDefault() /* keeps the textarea focused: the keyboard does not close */}
          onClick={submit}
          disabled={!!uploading || (!body.trim() && !files.length)}
          className="grid h-11 w-11 shrink-0 place-items-center rounded-full bg-accent text-bg disabled:opacity-40"
        >
          <Send size={18} />
        </button>
      </div>
      <input ref={photo} type="file" accept="image/*" multiple hidden onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
      <input ref={any} type="file" multiple hidden onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
      {picker && (
        <ActionSheet title={t("m.chat.attach")} onClose={() => setPicker(false)}>
          <SheetButton icon={<Camera size={20} />} onClick={() => photo.current?.click()}>{t("m.chat.photo")}</SheetButton>
          <SheetButton icon={<FileText size={20} />} onClick={() => any.current?.click()}>{t("m.chat.file")}</SheetButton>
        </ActionSheet>
      )}
    </div>
  );
}

export default function Conversation() {
  const { id: idParam } = useParams();
  const id = Number(idParam);
  const [params, setParams] = useSearchParams();
  const threadParam = Number(params.get("thread")) || null;
  const navigate = useNavigate();
  const [channel, setChannel] = useState<Channel | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [threadMsgs, setThreadMsgs] = useState<ChatMessage[]>([]);
  const [acting, setActing] = useState<ChatMessage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const members = useMembers();
  const presence = usePresence();
  const me = members.find((m) => m.is_owner)?.id ?? 0;
  const names = useMemo(() => members.map((m) => m.name), [members]);
  const isDm = channel?.kind === "dm";
  // A DM has no threads (an old link with ?thread= opens the conversation itself).
  const thread = channel && !isDm ? threadParam : null;
  const threadRef = useRef(thread);
  threadRef.current = thread;

  const markRead = useCallback((upTo?: number) => {
    chatApi.read(id, upTo).then(() => window.dispatchEvent(new Event("pos:needs-me")), () => undefined);
  }, [id]);
  const markThread = useCallback((root: number, upTo?: number) => {
    chatApi.threadRead(root, upTo).then(() => window.dispatchEvent(new Event("pos:threads")), () => undefined);
    setMessages((ms) => ms.map((x) => (x.id === root && x.thread ? { ...x, thread: { ...x.thread, unread: 0 } } : x)));
  }, []);

  const load = useCallback(() => {
    chatApi.channel(id).then(setChannel, (e) => setError(e.message));
    chatApi.messages(id).then((p) => {
      setMessages(p.messages);
      setHasMore(p.has_more);
      markRead();
    }, (e) => setError(e.message));
  }, [id, markRead]);
  useEffect(load, [load]);
  useEffect(() => {
    window.addEventListener("pos:resume", load);
    return () => window.removeEventListener("pos:resume", load);
  }, [load]);

  // A thread opens with all of it (also one whose root is not on the loaded page, from "Vlákna").
  useEffect(() => {
    setThreadMsgs([]);
    if (!thread) return;
    chatApi.thread(id, thread).then((p) => {
      setThreadMsgs(p.messages);
      markThread(thread);
    }, (e) => setError(e.message));
  }, [id, thread, markThread]);

  const received = useCallback(
    (m: ChatMessage) => {
      setMessages((ms) => (m.reply_to ? applyReply(upsert(ms, m), m, me, threadRef.current) : upsert(ms, m)));
      const open = threadRef.current;
      if (open && (m.reply_to === open || m.id === open)) setThreadMsgs((ms) => upsert(ms, m));
    },
    [me],
  );

  useEffect(
    () =>
      onChatEvent((ev) => {
        if (!isMessageEvent(ev) || ev.channel_id !== id) return;
        const m = ev.message;
        if (ev.type === "archive") {
          setMessages((ms) => ms.filter((x) => x.id !== m.id));
          setThreadMsgs((ms) => ms.filter((x) => x.id !== m.id));
          return;
        }
        received(m);
        if (ev.type === "message" && document.visibilityState === "visible") {
          markRead(m.id);
          const open = threadRef.current;
          if (open && m.reply_to === open) markThread(open, m.id);
        }
      }),
    [id, received, markRead, markThread],
  );

  const { pending, send, retry, discard } = useSender(channel?.id ?? null, received);

  const older = useCallback(() => {
    if (!messages.length) return Promise.resolve();
    return chatApi.messages(id, messages[0].id).then((p) => {
      setMessages((ms) => [...p.messages, ...ms]);
      setHasMore(p.has_more);
    });
  }, [id, messages]);

  const other = isDm ? channel?.members.find((m) => m.id !== me) : undefined;
  const otherFull = other ? members.find((m) => m.id === other.id) : undefined;
  const typingAll = (presence.typing[String(id)] ?? []).filter((e) => e.id !== me);
  const typing = isDm ? typingAll : typingAll.filter((e) => (thread ? e.thread === thread : e.thread === null));
  const title = channel ? (channel.kind === "group" ? `#${channel.name}` : channel.title) : "…";
  const sub = other
    ? other.kind !== "human"
      ? workLabel(otherFull, presence.working.includes(other.id)) ?? ""
      : ""
    : channel?.kind === "group"
      ? t("chat.members", { n: channel.members.length, vis: t(`chat.vis_short.${channel.visibility}`) })
      : "";
  const canWrite = !!channel && (channel.member || channel.kind === "group") && channel.name !== "system";
  const root = thread ? threadMsgs.find((m) => m.id === thread) ?? messages.find((m) => m.id === thread) ?? null : null;

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setActing(null);
    try {
      await fn();
      if (done) toast(done);
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  };
  const createTask = (m: ChatMessage) =>
    act(async () => {
      const first = visibleBody(m).split("\n").find((l) => l.trim()) ?? m.body;
      const task = await api<Task>("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          title: first.replace(/[*_`#>]/g, "").trim().slice(0, 120),
          notes: `${t("m.chat.task_from", { name: m.author_name, id: m.id })}\n\n${m.body}`,
        }),
      });
      toast(t("m.chat.task_created", { ref: task.ref }));
    }, "");
  const approve = (m: ChatMessage) => {
    const ref = APPROVAL_REF.exec(m.body);
    if (ref) return act(() => agentsApi.decide(Number(ref[1]), true), t("m.chat.approved"));
    setActing(null);
    send(t("m.chat.act.approve_reply"), [], isDm ? m.id : m.reply_to ?? m.id);
  };

  const openThread = (root: number) => setParams({ thread: String(root) });

  return (
    <div className="fixed inset-0 flex h-dvh flex-col bg-bg">
      <TopBar
        icon={isDm && other ? <Avatar name={channel?.title ?? other.name} size={36} human={other.kind === "human"} /> : undefined}
        title={thread ? t("m.chat.thread") : title}
        sub={thread ? (channel ? t("m.chat.thread_in", { name: channel.name ?? "" }) : "") : sub ? <span className={other && sub ? "text-accent" : ""}>{sub}</span> : undefined}
        back={() => (thread ? setParams({}) : navigate("/m"))}
      />
      {error && <p className="px-4 py-2 text-sm text-red-400">{error}</p>}
      {channel && (
        <Messenger
          mode={isDm ? "dm" : thread ? "thread" : "channel"}
          viewKey={`${id}:${thread ?? ""}`}
          messages={thread ? threadMsgs : messages}
          root={root}
          rootChannel={channel.name ?? undefined}
          me={me}
          names={names}
          pending={pending.filter((p) => (thread ? p.reply_to === thread : isDm || p.reply_to === null))}
          typing={typing}
          hasMore={!thread && hasMore}
          onOlder={older}
          onTap={setActing}
          onOpenThread={openThread}
          onRetry={retry}
          onDiscard={discard}
          empty={<p className="px-4 py-10 text-center text-sm text-ink-2">{t("m.chat.no_messages")}</p>}
        />
      )}
      {!channel && <div className="flex-1" />}
      {channel && canWrite && (
        <Composer
          channel={channel}
          replyTo={thread}
          placeholder={thread ? t("m.chat.reply_placeholder") : t("m.chat.placeholder", { name: title })}
          onSend={(body, files) => send(body, files, thread)}
        />
      )}
      {acting && (
        <ActionSheet title={t("m.chat.actions")} onClose={() => setActing(null)}>
          {!thread && !isDm && (
            <SheetButton icon={<MessageSquare size={20} />} onClick={() => { openThread(acting.reply_to ?? acting.id); setActing(null); }}>
              {t("m.chat.in_thread")}
            </SheetButton>
          )}
          {acting.author_id !== me && (
            <>
              <SheetButton icon={<ListPlus size={20} />} onClick={() => createTask(acting)}>{t("m.chat.act.task")}</SheetButton>
              <SheetButton icon={<CheckCircle2 size={20} />} onClick={() => approve(acting)}>{t("m.chat.act.approve")}</SheetButton>
            </>
          )}
          <SheetButton icon={<span className="text-[18px]">👍</span>} onClick={() => act(() => chatApi.react(acting.id, "👍"), "👍")}>
            {t("chat.react")}
          </SheetButton>
          <SheetButton icon={<Copy size={20} />} onClick={() => act(() => navigator.clipboard.writeText(visibleBody(acting)), t("m.chat.copied"))}>
            {t("m.chat.act.copy")}
          </SheetButton>
        </ActionSheet>
      )}
    </div>
  );
}
