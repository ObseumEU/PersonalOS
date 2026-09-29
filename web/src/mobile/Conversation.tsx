import { Camera, CheckCircle2, Copy, FileText, ListPlus, MessageSquare, Paperclip, Send, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { agentsApi } from "../agentsApi";
import { api } from "../api";
import { type Channel, type ChatMessage, chatApi } from "../chatApi";
import { toast } from "../components/overlay";
import { filesApi } from "../filesApi";
import { t } from "../i18n/core";
import { type Task } from "../tasksApi";
import { useMembers, workLabel } from "./ChatList";
import { isMessageEvent, onChatEvent, usePresence } from "./live";
import { ActionSheet, Avatar, Body, Dots, SheetButton, TopBar, when } from "./ui";

type FileAtt = { type: "file"; id: number; name: string; mime?: string | null; preview?: string };
const APPROVAL_REF = /schválení #(\d+)/i;
const PRIORITY_CLS: Record<string, string> = { change_plan: "border-accent/60 text-accent", stop: "border-amber-400/70 text-amber-300", fyi: "border-line text-ink-2" };

function Attachments({ m }: { m: ChatMessage }) {
  const files = m.attachments.filter((a) => a.type === "file") as unknown as FileAtt[];
  if (!files.length) return null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      {files.map((f) =>
        f.preview === "image" || f.mime?.startsWith("image/") ? (
          <a key={f.id} href={filesApi.contentUrl(f.id)} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
            <img src={filesApi.contentUrl(f.id)} alt={f.name} loading="lazy" className="max-h-56 max-w-[70vw] rounded-lg border border-line object-cover" />
          </a>
        ) : (
          <a
            key={f.id}
            href={filesApi.contentUrl(f.id)}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="flex h-10 items-center gap-2 rounded-lg border border-line bg-bg/60 px-3 text-[13px]"
          >
            <FileText size={16} className="text-ink-2" /> <span className="max-w-[50vw] truncate">{f.name}</span>
          </a>
        ),
      )}
    </div>
  );
}

/** The body without the "📎 name (soubor #12)" lines the server adds for agents (the chips show them). */
const visibleBody = (m: ChatMessage) => (m.attachments.some((a) => a.type === "file") ? m.body.replace(/^📎 .+ \(soubor #\d+\)$/gm, "").trim() : m.body);

function Bubble({
  m, mine, showName, names, replies, onTap, onThread,
}: {
  m: ChatMessage;
  mine: boolean;
  showName: boolean;
  names: string[];
  replies?: number;
  onTap: () => void;
  onThread?: () => void;
}) {
  const text = visibleBody(m);
  return (
    <div className={`flex gap-2 px-3 py-1 ${mine ? "flex-row-reverse" : ""}`}>
      {!mine && (showName ? <Avatar name={m.author_name} size={30} human={m.author_kind === "human"} /> : <span className="w-[30px] shrink-0" />)}
      <div className={`flex max-w-[82%] min-w-0 flex-col ${mine ? "items-end" : "items-start"}`}>
        {showName && !mine && (
          <span className="mb-0.5 flex flex-wrap items-center gap-1.5 px-1 text-[12px]">
            <span className={m.author_kind === "human" ? "text-ink" : "text-accent"}>{m.author_name}</span>
            {m.meeting && <span className="rounded-[3px] border border-line px-1 text-[11px] text-ink-2">{m.meeting.kind === "decision" ? t("chat.meeting_decision") : t("chat.meeting")}</span>}
          </span>
        )}
        <button
          onClick={onTap}
          aria-label={t("m.chat.actions")}
          className={`rounded-2xl px-3.5 py-2 text-left ${mine ? "rounded-br-md bg-accent/15 text-ink" : "rounded-bl-md border border-line bg-surface"} ${m.meeting?.kind === "decision" ? "border-emerald-400/60!" : ""}`}
        >
          {m.priority && m.priority !== "fyi" && (
            <span className={`mb-1 inline-block rounded-[3px] border px-1 text-[11px] ${PRIORITY_CLS[m.priority]}`}>{t(`priority.${m.priority}`)}</span>
          )}
          {text && <Body text={text} names={names} />}
          <Attachments m={m} />
          <span className="mt-0.5 block text-right text-[11px] text-ink-2">
            {when(m.created_at)}
            {m.edited_at ? ` · ${t("chat.edited")}` : ""}
          </span>
        </button>
        {(m.reactions.length > 0 || (replies ?? 0) > 0) && (
          <span className="mt-0.5 flex flex-wrap items-center gap-1.5 px-1">
            {m.reactions.map((r) => (
              <span key={r.emoji} className="rounded-full border border-line px-1.5 text-[12px]">
                {r.emoji} {r.count > 1 ? r.count : ""}
              </span>
            ))}
            {(replies ?? 0) > 0 && onThread && (
              <button onClick={onThread} className="flex h-8 items-center gap-1 text-[13px] text-accent">
                <MessageSquare size={13} /> {t("m.chat.replies", { n: replies })}
              </button>
            )}
          </span>
        )}
      </div>
    </div>
  );
}

function Composer({ channel, replyTo, placeholder, onSent }: { channel: Channel; replyTo: number | null; placeholder: string; onSent: () => void }) {
  const [body, setBody] = useState("");
  const [files, setFiles] = useState<FileAtt[]>([]);
  const [uploading, setUploading] = useState<string | null>(null);
  const [picker, setPicker] = useState(false);
  const [busy, setBusy] = useState(false);
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
  const upload = async (list: FileList | null) => {
    setPicker(false);
    for (const f of Array.from(list ?? [])) {
      setUploading(f.name);
      try {
        const up = await filesApi.upload(f);
        setFiles((fs) => [...fs, { type: "file", id: up.id, name: up.name, mime: up.mime, preview: up.preview }]);
      } catch (e) {
        toast(e instanceof Error ? e.message : String(e), { error: true });
      }
    }
    setUploading(null);
  };
  const submit = async () => {
    const text = body.trim();
    if ((!text && !files.length) || busy) return;
    setBusy(true);
    try {
      await api(`/api/chat/channels/${channel.id}/messages`, {
        method: "POST",
        body: JSON.stringify({ body: text, reply_to: replyTo, attachments: files.map((f) => ({ type: "file", id: f.id })) }),
      });
      setBody("");
      setFiles([]);
      requestAnimationFrame(grow);
      onSent();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
    }
  };
  const key = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (desktop && e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="border-t border-line bg-bg px-2 pt-2 pb-[max(8px,env(safe-area-inset-bottom))]">
      {(files.length > 0 || uploading) && (
        <div className="flex flex-wrap gap-1.5 px-1 pb-2">
          {files.map((f) => (
            <span key={f.id} className="flex h-8 items-center gap-1.5 rounded-full border border-line bg-surface pr-1 pl-3 text-[13px]">
              <span className="max-w-[40vw] truncate">{f.name}</span>
              <button aria-label={t("m.chat.remove_attachment")} onClick={() => setFiles((fs) => fs.filter((x) => x.id !== f.id))} className="grid h-7 w-7 place-items-center text-ink-2">
                <X size={14} />
              </button>
            </span>
          ))}
          {uploading && <span className="flex h-8 items-center text-[13px] text-ink-2">{t("m.chat.uploading", { name: uploading })}</span>}
        </div>
      )}
      <div className="flex items-end gap-1.5">
        <button aria-label={t("m.chat.attach")} onClick={() => setPicker(true)} className="grid h-11 w-11 shrink-0 place-items-center rounded-full text-ink-2 active:bg-raised">
          <Paperclip size={20} />
        </button>
        <textarea
          ref={area}
          rows={1}
          value={body}
          onChange={(e) => change(e.target.value)}
          onKeyDown={key}
          placeholder={placeholder}
          aria-label={t("chat.message")}
          enterKeyHint={desktop ? "send" : "enter"}
          className="max-h-[140px] min-h-11 min-w-0 flex-1 resize-none rounded-3xl border border-line bg-surface px-4 py-2.5 text-[16px] leading-snug outline-none focus:border-accent"
        />
        <button
          aria-label={t("m.chat.send")}
          onClick={submit}
          disabled={busy || (!body.trim() && !files.length)}
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
  const thread = Number(params.get("thread")) || null;
  const navigate = useNavigate();
  const [channel, setChannel] = useState<Channel | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [acting, setActing] = useState<ChatMessage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const list = useRef<HTMLDivElement>(null);
  const members = useMembers();
  const presence = usePresence();
  const me = members.find((m) => m.is_owner)?.id ?? 0;
  const names = useMemo(() => members.map((m) => m.name), [members]);

  const toBottom = () => requestAnimationFrame(() => list.current?.scrollTo({ top: list.current.scrollHeight }));
  const markRead = useCallback((upTo?: number) => {
    chatApi.read(id, upTo).then(() => window.dispatchEvent(new Event("pos:needs-me")), () => undefined);
  }, [id]);

  const load = useCallback(() => {
    chatApi.channel(id).then(setChannel, (e) => setError(e.message));
    chatApi.messages(id).then((p) => {
      setMessages(p.messages);
      setHasMore(p.has_more);
      toBottom();
      markRead();
    }, (e) => setError(e.message));
  }, [id, markRead]);
  useEffect(load, [load]);
  useEffect(() => {
    window.addEventListener("pos:resume", load);
    return () => window.removeEventListener("pos:resume", load);
  }, [load]);
  useEffect(() => {
    toBottom();
  }, [thread]);

  useEffect(
    () =>
      onChatEvent((ev) => {
        if (!isMessageEvent(ev) || ev.channel_id !== id) return;
        const m = ev.message;
        if (ev.type === "archive") {
          setMessages((ms) => ms.filter((x) => x.id !== m.id));
          return;
        }
        const el = list.current;
        const atBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 120;
        setMessages((ms) => {
          const known = ms.some((x) => x.id === m.id);
          let next = known ? ms.map((x) => (x.id === m.id ? m : x)) : [...ms, m];
          if (!known && m.reply_to) next = next.map((x) => (x.id === m.reply_to ? { ...x, replies: x.replies + 1 } : x));
          return next;
        });
        if (ev.type === "message") {
          if (atBottom || m.author_id === me) toBottom();
          if (document.visibilityState === "visible") markRead(m.id);
        }
      }),
    [id, me, markRead],
  );

  const older = () => {
    if (!messages.length) return;
    const el = list.current;
    const h = el?.scrollHeight ?? 0;
    chatApi.messages(id, messages[0].id).then((p) => {
      setMessages((ms) => [...p.messages, ...ms]);
      setHasMore(p.has_more);
      requestAnimationFrame(() => el && el.scrollTo({ top: el.scrollHeight - h }));
    });
  };

  const other = channel?.kind === "dm" ? channel.members.find((m) => m.id !== me) : undefined;
  const otherFull = other ? members.find((m) => m.id === other.id) : undefined;
  const typing = (presence.typing[String(id)] ?? []).filter((e) => e.id !== me && (!thread || e.thread === thread || e.thread === null));
  const busyHere = channel?.members.filter((m) => m.kind !== "human" && m.id !== me && presence.working.includes(m.id)) ?? [];
  const sub = typing.length
    ? `${typing.map((e) => e.name).join(", ")} ${typing.some((e) => e.state === "typing") ? t("m.chat.typing") : t("m.chat.working")}`
    : other && other.kind !== "human"
      ? workLabel(otherFull, presence.working.includes(other.id)) ?? ""
      : channel?.kind === "group"
        ? t("chat.members", { n: channel.members.length, vis: t(`chat.vis_short.${channel.visibility}`) })
        : "";

  const byId = new Map(messages.map((m) => [m.id, m]));
  const shown = thread ? messages.filter((m) => m.id === thread || m.reply_to === thread) : messages.filter((m) => !m.reply_to || !byId.has(m.reply_to));
  const canWrite = !!channel && (channel.member || channel.kind === "group") && channel.name !== "system";
  const title = channel ? (channel.kind === "group" ? `#${channel.name}` : channel.title) : "…";

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
    return act(() => chatApi.send(id, t("m.chat.act.approve_reply"), m.reply_to ?? m.id), t("m.chat.approval_sent"));
  };

  return (
    <div className="fixed inset-0 flex flex-col bg-bg">
      <TopBar
        title={thread ? t("m.chat.thread") : title}
        sub={thread ? title : sub}
        back={() => (thread ? setParams({}) : navigate("/m"))}
      />
      <div ref={list} className="min-h-0 flex-1 overflow-y-auto overscroll-contain py-2">
        {error && <p className="px-4 py-2 text-sm text-red-400">{error}</p>}
        {hasMore && (
          <button onClick={older} className="mx-auto block h-10 px-4 text-[13px] text-ink-2">
            {t("m.chat.load_older")}
          </button>
        )}
        {channel && shown.length === 0 && <p className="px-4 py-8 text-center text-sm text-ink-2">{t("m.chat.no_messages")}</p>}
        {shown.map((m, i) => {
          const prev = shown[i - 1];
          const grouped = prev && prev.author_id === m.author_id && new Date(m.created_at).getTime() - new Date(prev.created_at).getTime() < 5 * 60000;
          return (
            <div key={m.id} className={thread && m.id === thread ? "border-b border-line pb-2 mb-1" : ""}>
              <Bubble
                m={m}
                mine={m.author_id === me}
                showName={!grouped || (thread !== null && m.id === thread)}
                names={names}
                replies={thread ? 0 : m.replies}
                onTap={() => setActing(m)}
                onThread={() => setParams({ thread: String(m.id) })}
              />
            </div>
          );
        })}
      </div>
      <div className="flex h-6 items-center gap-2 px-4 text-[12px] text-ink-2" role="status" aria-live="polite">
        {typing.length > 0 ? (
          <>
            <span className="text-accent"><Dots soft={typing.every((e) => e.state === "working")} /></span>
            <span className="truncate">{sub}</span>
          </>
        ) : busyHere.length > 0 && channel?.kind === "group" ? (
          <span className="truncate">{t("chat.working_here", { names: busyHere.map((m) => m.name).join(", ") })}</span>
        ) : null}
      </div>
      {channel && canWrite && (
        <Composer
          channel={channel}
          replyTo={thread}
          placeholder={thread ? t("m.chat.reply_placeholder") : t("m.chat.placeholder", { name: title })}
          onSent={toBottom}
        />
      )}
      {acting && (
        <ActionSheet title={t("m.chat.actions")} onClose={() => setActing(null)}>
          {!thread && (
            <SheetButton icon={<MessageSquare size={20} />} onClick={() => { setParams({ thread: String(acting.reply_to ?? acting.id) }); setActing(null); }}>
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
          <SheetButton
            icon={<Copy size={20} />}
            onClick={() => act(() => navigator.clipboard.writeText(visibleBody(acting)), t("m.chat.copied"))}
          >
            {t("m.chat.act.copy")}
          </SheetButton>
        </ActionSheet>
      )}
    </div>
  );
}
