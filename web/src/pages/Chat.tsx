import { Archive, ArrowLeft, AtSign, Bell, ChevronDown, Eye, Hash, MessageSquare, MessagesSquare, Pencil, Pin, Plus, Send, SmilePlus, X } from "lucide-react";
import { TaskLink } from "../taskSheet";
import { ToolChip, stripToolMarkup } from "../toolMarkup";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { type OrgMember, agentsApi } from "../agentsApi";
import { type Channel, type ChatMember, type ChatMessage, type Presence, type Priority, type StreamEvent, type TypingEntry, chatApi } from "../chatApi";
import Messenger, { type FileAtt, ThreadChip, replyCount, useSender, visibleBody } from "../chat/Messenger";
import { FileCard } from "../files/FileCard";
import ThreadList, { useThreads } from "../chat/ThreadList";
import { applyReply, upsert as upsertMsg } from "../chat/timeline";
import { WorkingDot, WorkingOnText, workingOn } from "../components/agents/WorkingOn";
import { confirmDialog } from "../components/overlay";
import { PageHeader, Panel } from "../components/ui";
import { LOCALE, t } from "../i18n";

const EMOJI = ["👍", "✅", "👀", "🎉", "❤️", "🙏"];
const PRIORITY_CLS: Record<Priority, string> = {
  fyi: "border-line text-ink-2",
  change_plan: "border-accent/60 text-accent",
  stop: "border-amber-400/70 text-amber-300",
};
/** Channels for automated notices (collapsed in the rail, grouped when read). */
const SYSTEM_CHANNELS = new Set(["system"]);
const isSystem = (c: Channel) => c.kind === "group" && SYSTEM_CHANNELS.has(c.name ?? "");

function hhmm(iso: string) {
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString(LOCALE, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Light markdown: ```code```, `code`, **bold**, links, @mentions and T-123 task links. */
function Body({ text, names }: { text: string; names: string[] }) {
  const inline = useMemo(() => {
    const mention = names.length ? `@(?:${[...names].sort((a, b) => b.length - a.length).map(escapeRe).join("|")})` : "@\\w+";
    return new RegExp(`(\\[nástroj[^\\]\\n]*\\]|\`[^\`\\n]+\`|\\*\\*[^*\\n]+\\*\\*|https?://[^\\s<]+|\\bT-\\d{1,6}\\b|${mention})`, "gi");
  }, [names]);
  const renderInline = (s: string, key: string): ReactNode[] =>
    s.split(inline).map((part, i) => {
      const k = `${key}-${i}`;
      if (i % 2 === 0) return <Fragment key={k}>{part}</Fragment>;
      if (part.startsWith("[nástroj")) return <ToolChip key={k} label={part.slice(1, -1)} />;
      if (part.startsWith("`")) return <code key={k} className="rounded-[3px] bg-raised px-1 font-mono text-[12px]">{part.slice(1, -1)}</code>;
      if (part.startsWith("**")) return <strong key={k} className="font-medium">{part.slice(2, -2)}</strong>;
      if (part.startsWith("http")) return <a key={k} href={part} target="_blank" rel="noreferrer noopener" className="break-all text-accent underline decoration-accent/40">{part}</a>;
      if (/^T-\d+$/i.test(part)) return <TaskLink key={k} taskRef={part.toUpperCase()} className="rounded-[3px] bg-accent/10 px-1 font-mono text-[12px] text-accent">{part.toUpperCase()}</TaskLink>;
      return <span key={k} className="rounded-[3px] bg-accent/15 px-0.5 text-accent">{part}</span>;
    });
  // Tool calls a model wrote as text never ran: a muted chip instead (toolMarkup).
  const blocks = stripToolMarkup(text).split(/```(?:\w+\n)?/);
  return (
    <div className="text-[14px] leading-relaxed break-words whitespace-pre-wrap">
      {blocks.map((b, i) =>
        i % 2 === 1 ? (
          <pre key={i} className="my-1 overflow-x-auto rounded border border-line bg-bg p-2 font-mono text-[12px]">{b.replace(/\n$/, "")}</pre>
        ) : (
          <Fragment key={i}>{renderInline(b, String(i))}</Fragment>
        ),
      )}
    </div>
  );
}

function KindTag({ m }: { m: ChatMessage }) {
  if (m.trust === "external") return <span className="rounded-[3px] border border-amber-400/50 px-1 text-xs text-amber-300" title={t("chat.external_title")}>{t("chat.external")}</span>;
  if (m.trust === "agent") return <span className="rounded-[3px] border border-line px-1 text-xs text-ink-2" title={t("chat.agent_title")}>{t("who.agent")}</span>;
  return null;
}

function MeetingTag({ m }: { m: ChatMessage }) {
  const mark = m.meeting;
  if (!mark) return null;
  if (mark.kind === "decision")
    return <span className="rounded-[3px] border border-emerald-400/60 bg-emerald-400/10 px-1 text-xs text-emerald-300">{t("chat.meeting_decision")}</span>;
  if (mark.kind === "agenda")
    return <span className="rounded-[3px] border border-accent/60 px-1 text-xs text-accent">{t("chat.meeting")}</span>;
  return <span className="rounded-[3px] border border-line px-1 text-xs text-ink-2">{t("chat.meeting_round", { n: mark.round ?? 1, of: mark.rounds ?? 1 })}</span>;
}

/** Round markers in a meeting thread: a divider where the next round starts. */
function RoundDivider({ prev, m }: { prev?: ChatMessage; m: ChatMessage }) {
  const r = m.meeting?.round;
  if (!r || m.meeting?.kind === "agenda" || m.meeting?.kind === "decision" || prev?.meeting?.round === r) return null;
  return (
    <div className="mx-4 my-1 flex items-center gap-2 text-[11px] uppercase tracking-wide text-ink-2">
      <span className="h-px flex-1 bg-line" />
      {t(r === 1 ? "chat.meeting_round1" : "chat.meeting_roundN", { n: r })}
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}

function TypingDots({ soft = false }: { soft?: boolean }) {
  return (
    <span className={`typing-dots ${soft ? "soft" : ""}`} aria-hidden="true">
      <span />
      <span />
      <span />
    </span>
  );
}

/** "Hlídač píše", "Hlídač a CEO píšou", "3 lidé píšou", "5 lidí píše". */
function who(list: TypingEntry[], verb: "typing" | "working") {
  const n = list.length;
  const v = (k: "one" | "few" | "many") => t(`chat.${verb}.${k}`);
  if (n === 1) return `${list[0].name} ${v("one")}`;
  if (n === 2) return `${list[0].name} ${t("chat.and")} ${list[1].name} ${v("few")}`;
  return n <= 4 ? `${n} ${t("chat.people_few")} ${v("few")}` : `${n} ${t("chat.people_many")} ${v("many")}`;
}

function typingLabel(entries: TypingEntry[]) {
  const typing = entries.filter((e) => e.state === "typing");
  const working = entries.filter((e) => e.state === "working");
  return [typing.length ? `${who(typing, "typing")}…` : "", working.length ? `${who(working, "working")}…` : ""].filter(Boolean).join(" · ");
}

/** Under a message list: who is typing (or working on a reply). Always takes its line, so the list does not jump. */
function TypingLine({ entries }: { entries: TypingEntry[] }) {
  const label = typingLabel(entries);
  const soft = entries.length > 0 && entries.every((e) => e.state === "working");
  return (
    <div className="flex h-5 items-center gap-2 px-4 text-[12px] text-ink-2" aria-live="polite" role="status">
      {label && (
        <>
          <span className="text-accent"><TypingDots soft={soft} /></span>
          <span className="truncate">{label}</span>
        </>
      )}
    </div>
  );
}

function MessageItem({
  m, me, names, parent, compact, onReply, onReact, onEdit, onArchive, onOpenThread,
}: {
  m: ChatMessage;
  me: number;
  names: string[];
  parent?: ChatMessage;
  compact?: boolean;
  onReply: () => void;
  onReact: (emoji: string) => void;
  onEdit: (body: string) => void;
  onArchive: () => void;
  onOpenThread?: () => void;
}) {
  const [picking, setPicking] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const mine = m.author_id === me;
  const mentioned = m.mentions.includes(me);
  return (
    // Focusable, so a tap on a phone shows the actions (no hover there).
    <div tabIndex={0} className={`group relative flex flex-col gap-1 px-4 py-2 outline-none hover:bg-raised/50 focus-within:bg-raised/50 ${m.meeting?.kind === "decision" ? "border-l-2 border-emerald-400/70 bg-emerald-400/5" : mentioned ? "shadow-[inset_2px_0_0_var(--color-accent)]" : ""}`}>
      {parent && !compact && (
        <button onClick={onOpenThread} className="flex min-w-0 items-center gap-1 text-left text-xs text-ink-2 hover:text-ink">
          ↳ <span className="shrink-0 whitespace-nowrap">{parent.author_name}</span>
          <span className="truncate">{parent.body.slice(0, 90)}</span>
        </button>
      )}
      <div className="flex flex-wrap items-baseline gap-2">
        <span className={`text-[13px] font-medium ${m.author_kind === "human" ? "text-ink" : "text-accent"}`}>{m.author_name}</span>
        <KindTag m={m} />
        <MeetingTag m={m} />
        {m.priority && <span className={`rounded-[3px] border px-1 text-xs ${PRIORITY_CLS[m.priority]}`}>{t(`priority.${m.priority}`)}</span>}
        <span className="text-xs text-ink-2">
          {hhmm(m.created_at)}
          {m.edited_at ? ` · ${t("chat.edited")}` : ""}
        </span>
      </div>
      {editing !== null ? (
        <form
          className="flex flex-col gap-1.5"
          onSubmit={(e) => {
            e.preventDefault();
            if (editing.trim()) onEdit(editing.trim());
            setEditing(null);
          }}
        >
          <textarea autoFocus rows={2} value={editing} onChange={(e) => setEditing(e.target.value)} aria-label={t("chat.edit")} className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent" />
          <span className="flex gap-2">
            <button className="btn-accent">{t("act.save")}</button>
            <button type="button" className="btn" onClick={() => setEditing(null)}>{t("act.cancel")}</button>
          </span>
        </form>
      ) : (
        <>
          <Body text={visibleBody(m)} names={names} />
          {m.attachments.some((a) => a.type === "file") && (
            <div className="flex flex-col gap-1.5">
              {(m.attachments.filter((a) => a.type === "file") as unknown as FileAtt[]).map((f) => (
                <FileCard key={`${f.id}-${f.version ?? ""}`} file={f} />
              ))}
            </div>
          )}
        </>
      )}
      {m.reactions.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          {m.reactions.map((r) => (
            <button
              key={r.emoji}
              onClick={() => onReact(r.emoji)}
              className={`flex h-6 items-center gap-1 rounded-full border px-2 text-[12px] ${r.actors.includes(me) ? "border-accent/60 bg-accent/10" : "border-line"}`}
            >
              {r.emoji} <span className="font-mono text-xs text-ink-2">{r.count}</span>
            </button>
          ))}
        </div>
      )}
      {m.replies > 0 && !compact && onOpenThread && <ThreadChip count={m.replies} thread={m.thread} onOpen={onOpenThread} />}
      <div className="absolute top-1 right-3 hidden items-center gap-0.5 rounded border border-line bg-surface p-0.5 group-hover:flex group-focus-within:flex">
        <button aria-label={t("chat.reply_thread")} title={t("chat.reply_thread")} onClick={onReply} className="p-1 text-ink-2 hover:text-accent"><MessageSquare size={14} /></button>
        <button aria-label={t("chat.react")} title={t("chat.react")} onClick={() => setPicking((p) => !p)} className="p-1 text-ink-2 hover:text-accent"><SmilePlus size={14} /></button>
        {mine && <button aria-label={t("chat.edit")} title={t("chat.edit_title")} onClick={() => setEditing(m.body)} className="p-1 text-ink-2 hover:text-accent"><Pencil size={14} /></button>}
        <button
          aria-label={t("act.archive")}
          title={t("chat.archive_title")}
          onClick={() => confirmDialog({ title: t("chat.archive_confirm"), body: t("chat.archive_body"), confirm: t("act.archive") }).then((ok) => ok !== null && onArchive())}
          className="p-1 text-ink-2 hover:text-amber-300"
        >
          <Archive size={14} />
        </button>
      </div>
      {picking && (
        <div className="absolute top-8 right-3 z-10 flex gap-0.5 rounded border border-line bg-surface p-1">
          {EMOJI.map((e) => (
            <button key={e} onClick={() => { onReact(e); setPicking(false); }} className="rounded px-1.5 py-0.5 text-[15px] hover:bg-raised">{e}</button>
          ))}
        </div>
      )}
    </div>
  );
}

/** #system: runs of notices from one sender, folded into one line each (open to read them all). */
function NoticeGroups({ messages, names }: { messages: ChatMessage[]; names: string[] }) {
  const groups = useMemo(() => {
    const out: ChatMessage[][] = [];
    for (const m of messages) {
      const last = out[out.length - 1];
      const prev = last?.[last.length - 1];
      if (prev && prev.author_id === m.author_id && new Date(m.created_at).getTime() - new Date(prev.created_at).getTime() < 60 * 60 * 1000) last.push(m);
      else out.push([m]);
    }
    return out;
  }, [messages]);
  const [open, setOpen] = useState<Set<number>>(new Set());
  return (
    <>
      {groups.map((g) => {
        const first = g[0];
        const last = g[g.length - 1];
        const expanded = open.has(first.id) || g.length === 1;
        return (
          <div key={first.id} className="border-b border-line/60 px-4 py-2">
            <button
              className="flex w-full min-w-0 items-baseline gap-2 text-left"
              aria-expanded={expanded}
              onClick={() => setOpen((s) => new Set(s.has(first.id) ? [...s].filter((x) => x !== first.id) : [...s, first.id]))}
            >
              <span className="text-[13px] font-medium text-accent">{first.author_name}</span>
              <span className="text-xs text-ink-2">
                {hhmm(first.created_at)}
                {g.length > 1 ? `–${hhmm(last.created_at)} · ${t("chat.notices", { n: g.length })}` : ""}
              </span>
              {g.length > 1 && <ChevronDown size={14} className={`ml-auto shrink-0 text-ink-2 transition ${expanded ? "rotate-180" : ""}`} />}
            </button>
            {expanded ? (
              <div className="mt-1 flex flex-col gap-1.5">
                {g.map((m) => (
                  <Body key={m.id} text={m.body} names={names} />
                ))}
              </div>
            ) : (
              <p className="mt-0.5 truncate text-[13px] text-ink-2">{last.body}</p>
            )}
          </div>
        );
      })}
    </>
  );
}

function Composer({
  channel, members, replyTo, onCancelReply, onSent, placeholder, send,
}: {
  channel: Channel;
  members: ChatMember[];
  replyTo?: ChatMessage | null;
  onCancelReply?: () => void;
  onSent?: () => void;
  placeholder?: string;
  /** Optimistic sending (the messenger view shows the bubble at once); else the composer posts and waits. */
  send?: (text: string, priority: Priority | null) => void;
}) {
  const [body, setBody] = useState("");
  const [priority, setPriority] = useState<Priority | "">("");
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState<string | null>(null);
  const [pick, setPick] = useState(0);
  const ref = useRef<HTMLTextAreaElement>(null);
  const lastTyping = useRef(0);
  const hasAgents = channel.members.some((m) => m.kind !== "human");
  // Members of the channel first, then everyone (mentioning an agent invites it).
  const candidates = useMemo(() => {
    if (query === null) return [];
    const q = query.toLowerCase();
    const inChannel = new Set(channel.members.map((m) => m.id));
    return members
      .filter((m) => m.name.toLowerCase().includes(q) && (channel.kind === "group" || inChannel.has(m.id)))
      .sort((a, b) => Number(inChannel.has(b.id)) - Number(inChannel.has(a.id)) || Number(!b.name.toLowerCase().startsWith(q)) - Number(!a.name.toLowerCase().startsWith(q)))
      .slice(0, 6);
  }, [query, members, channel]);

  const onChange = (value: string) => {
    setBody(value);
    const caret = ref.current?.selectionStart ?? value.length;
    const m = /(^|\s)@([^\s@]{0,24})$/.exec(value.slice(0, caret));
    setQuery(m ? m[2] : null);
    setPick(0);
    if (Date.now() - lastTyping.current > 3000) {
      lastTyping.current = Date.now();
      chatApi.typing(channel.id, replyTo?.id ?? null).catch(() => undefined);
    }
  };
  const complete = (m: ChatMember) => {
    const el = ref.current;
    const caret = el?.selectionStart ?? body.length;
    const before = body.slice(0, caret).replace(/@([^\s@]{0,24})$/, `@${m.name} `);
    const next = before + body.slice(caret);
    setBody(next);
    setQuery(null);
    requestAnimationFrame(() => {
      el?.focus();
      el?.setSelectionRange(before.length, before.length);
    });
  };
  const submit = () => {
    const text = body.trim();
    if (!text) return;
    if (send) {
      send(text, priority || null);
      setBody("");
      setPriority("");
      setError(null);
      onSent?.();
      return;
    }
    chatApi.send(channel.id, text, replyTo?.id ?? null, priority || null).then(
      () => {
        setBody("");
        setPriority("");
        setError(null);
        onSent?.();
      },
      (e) => setError(e.message),
    );
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (candidates.length) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setPick((p) => (p + (e.key === "ArrowDown" ? 1 : candidates.length - 1)) % candidates.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        complete(candidates[pick]);
        return;
      }
      if (e.key === "Escape") {
        setQuery(null);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="relative flex flex-col gap-1.5 border-t border-line p-3">
      {replyTo && (
        <span className="flex min-w-0 items-center gap-2 text-xs text-ink-2">
          <span className="shrink-0 whitespace-nowrap">↳ {t("chat.replying_to", { name: replyTo.author_name })}</span>
          <span className="truncate">{replyTo.body.slice(0, 60)}</span>
          {onCancelReply && <button aria-label={t("chat.cancel_reply")} onClick={onCancelReply} className="ml-auto text-ink-2 hover:text-ink"><X size={13} /></button>}
        </span>
      )}
      {candidates.length > 0 && (
        <div role="listbox" className="absolute bottom-full left-3 z-20 mb-1 w-64 max-w-[calc(100%-1.5rem)] rounded border border-line bg-surface py-1 shadow-lg">
          {candidates.map((m, i) => (
            <button
              key={m.id}
              role="option"
              aria-selected={i === pick}
              onMouseDown={(e) => { e.preventDefault(); complete(m); }}
              className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] ${i === pick ? "bg-raised text-ink" : "text-ink-2"}`}
            >
              <WorkingDot on={m.working} />
              <span className="truncate">{m.name}</span>
              <span className="ml-auto shrink-0 text-xs text-ink-2">{m.is_owner ? t("who.owner") : m.kind === "human" ? t("who.person") : m.remote ? t("who.remote") : t("who.agent")}</span>
            </button>
          ))}
        </div>
      )}
      <div className="flex items-end gap-2">
        <textarea
          ref={ref}
          rows={2}
          value={body}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKey}
          aria-label={t("chat.message")}
          placeholder={placeholder ?? t("chat.placeholder", { channel: channel.title })}
          className="min-w-0 flex-1 resize-none rounded border border-line bg-bg p-2 text-[14px] outline-none focus:border-accent"
        />
        <button className="btn-accent h-[38px]!" onClick={submit} aria-label={t("act.send")}>
          <Send size={14} />
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {hasAgents && (
          <label className="flex items-center gap-1.5 text-xs text-ink-2" title={t("priority.help")}>
            {t("priority.label")}
            <select value={priority} onChange={(e) => setPriority(e.target.value as Priority | "")} className="rounded border border-line bg-bg px-1.5 py-0.5 text-xs text-ink outline-none focus:border-accent">
              <option value="">{t("priority.none")}</option>
              <option value="fyi">{t("priority.fyi")}</option>
              <option value="change_plan">{t("priority.change_plan")}</option>
              <option value="stop">{t("priority.stop")}</option>
            </select>
          </label>
        )}
        <span className="ml-auto hidden text-xs text-ink-2 sm:inline">{t("chat.enter_hint")}</span>
      </div>
      {error && <span className="text-xs text-red-400">{error}</span>}
    </div>
  );
}

function NewChannel({ members, me, onCreated, onClose }: { members: ChatMember[]; me: number; onCreated: (c: Channel) => void; onClose: () => void }) {
  const [name, setName] = useState("");
  const [topic, setTopic] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  const [visibility, setVisibility] = useState("team");
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      className="flex flex-col gap-2 border-b border-line p-3"
      onSubmit={(e) => {
        e.preventDefault();
        chatApi.createChannel(name, picked, topic, visibility).then(onCreated, (err) => setError(err.message));
      }}
    >
      <span className="flex items-center text-xs font-medium text-ink-2">{t("chat.new_channel")} <button type="button" aria-label={t("act.close")} onClick={onClose} className="ml-auto"><X size={13} /></button></span>
      <input autoFocus value={name} onChange={(e) => setName(e.target.value)} aria-label={t("chat.channel_name")} placeholder="release-friday" className="rounded border border-line bg-bg px-2 py-1 text-[13px] outline-none focus:border-accent" />
      <input value={topic} onChange={(e) => setTopic(e.target.value)} aria-label={t("chat.topic")} placeholder={t("chat.topic_ph")} className="rounded border border-line bg-bg px-2 py-1 text-[13px] outline-none focus:border-accent" />
      <div className="flex max-h-32 flex-col overflow-y-auto">
        {members.filter((m) => m.id !== me).map((m) => (
          <label key={m.id} className="flex items-center gap-2 py-0.5 text-[13px] text-ink-2">
            <input type="checkbox" className="accent-accent" checked={picked.includes(m.id)} onChange={(e) => setPicked((p) => (e.target.checked ? [...p, m.id] : p.filter((x) => x !== m.id)))} />
            {m.name}
          </label>
        ))}
      </div>
      <select value={visibility} onChange={(e) => setVisibility(e.target.value)} aria-label={t("chat.visibility")} className="rounded border border-line bg-bg px-2 py-1 text-[12px] text-ink outline-none">
        <option value="team">{t("chat.vis.team")}</option>
        <option value="private">{t("chat.vis.private")}</option>
        <option value="public">{t("chat.vis.public")}</option>
      </select>
      <button className="btn-accent justify-center" disabled={!name.trim()}>{t("chat.create")}</button>
      {error && <span className="text-xs text-red-400">{error}</span>}
    </form>
  );
}

function RailItem({ c, active, working, typing, pinned, onClick }: { c: Channel; active: boolean; working: Set<number>; typing?: TypingEntry[]; pinned?: boolean; onClick: () => void }) {
  const others = c.members.filter((m) => !m.is_owner);
  const busy = c.kind === "dm" && others.some((m) => working.has(m.id));
  return (
    <button
      onClick={onClick}
      aria-current={active ? "true" : undefined}
      className={`flex h-[34px] w-full min-w-0 items-center gap-2 rounded px-2.5 text-left text-[13px] transition ${
        active ? "bg-raised text-ink shadow-[inset_2px_0_0_var(--color-accent)]" : c.unread ? "text-ink hover:bg-raised" : "text-ink-2 hover:bg-raised"
      }`}
    >
      {c.kind === "group" ? isSystem(c) ? <Bell size={14} className="shrink-0 text-ink-2" /> : <Hash size={14} className="shrink-0 text-ink-2" /> : <WorkingDot on={busy} />}
      <span className={`truncate ${c.unread ? "font-medium" : ""}`}>{c.kind === "group" ? c.name : c.title}</span>
      {pinned && <Pin size={12} className="shrink-0 text-accent" aria-label={t("chat.pinned")} />}
      {typing && typing.length > 0 && (
        <span className="shrink-0 text-accent" title={typingLabel(typing)} aria-label={typingLabel(typing)}><TypingDots /></span>
      )}
      {c.mentions > 0 && <AtSign size={12} className="shrink-0 text-accent" />}
      {c.unread > 0 && <span className="ml-auto rounded-sm bg-accent/15 px-1.5 font-mono text-xs text-accent">{c.unread}</span>}
    </button>
  );
}

// "Vlákna" reloads on "pos:threads": the page's stream sends it when a reply comes in anywhere.
const onReplyEvent = () => () => undefined;

const railHeading = "flex items-center gap-1.5 px-2.5 pt-3 pb-1 text-xs font-medium text-ink-2";

export default function Chat() {
  const [params, setParams] = useSearchParams();
  const current = Number(params.get("c")) || null;
  const [channels, setChannels] = useState<Channel[]>([]);
  const [members, setMembers] = useState<ChatMember[]>([]);
  const [org, setOrg] = useState<OrgMember[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [presence, setPresence] = useState<Presence>({ working: [], typing: {} });
  const thread = Number(params.get("t")) || null;
  const threadsView = params.get("v") === "threads";
  const [threadMsgs, setThreadMsgs] = useState<ChatMessage[]>([]);
  const [showAll, setShowAll] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [systemOpen, setSystemOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const currentRef = useRef(current);
  currentRef.current = current;
  const meRef = useRef(0);

  const me = members.find((m) => m.is_owner)?.id ?? 0;
  meRef.current = me;
  const threads = useThreads(onReplyEvent);
  const channel = channels.find((c) => c.id === current) ?? null;
  const names = useMemo(() => members.map((m) => m.name), [members]);
  const working = useMemo(() => new Set(presence.working), [presence]);
  const byId = useMemo(() => new Map(messages.map((m) => [m.id, m])), [messages]);

  const loadChannels = useCallback(() => {
    chatApi.channels(showAll).then(setChannels, (e) => setError(e.message));
  }, [showAll]);
  useEffect(loadChannels, [loadChannels]);
  useEffect(() => {
    chatApi.members().then(setMembers, () => undefined);
    agentsApi.org().then((o) => setOrg(o.members), () => undefined);
  }, []);

  // ?dm=<member id> opens (or creates) the DM with that member.
  useEffect(() => {
    const dm = Number(params.get("dm"));
    if (!dm) return;
    chatApi.dm(dm).then((c) => {
      setChannels((cs) => (cs.some((x) => x.id === c.id) ? cs : [c, ...cs]));
      setParams({ c: String(c.id) }, { replace: true });
    }, (e) => setError(e.message));
  }, [params, setParams]);

  // The CEO is the owner's single channel: its DM opens by default on desktop ("Zeptej se CEO"); without a CEO, #team.
  const ceoMember = members.find((m) => m.is_ceo);
  useEffect(() => {
    if (!current && !params.get("dm") && !params.get("v") && channels.length && members.length && window.matchMedia("(min-width: 768px)").matches) {
      const dm = ceoMember ? channels.find((c) => c.kind === "dm" && c.member && c.members.some((m) => m.id === ceoMember.id)) : undefined;
      if (dm) setParams({ c: String(dm.id) }, { replace: true });
      else if (ceoMember) setParams({ dm: String(ceoMember.id) }, { replace: true });
      else {
        const team = channels.find((c) => c.name === "team") ?? channels.find((c) => !isSystem(c)) ?? channels[0];
        setParams({ c: String(team.id) }, { replace: true });
      }
    }
  }, [current, channels, members.length, params, setParams, ceoMember]);

  const scrollDown = () => requestAnimationFrame(() => listRef.current?.scrollTo({ top: listRef.current.scrollHeight }));
  const markRead = useCallback((id: number, upTo?: number) => {
    chatApi.read(id, upTo).then(() => {
      setChannels((cs) => cs.map((c) => (c.id === id ? { ...c, unread: 0, mentions: 0 } : c)));
      window.dispatchEvent(new Event("pos:needs-me")); // read mentions leave "Čeká na tebe"
    }, () => undefined);
  }, []);

  const setThread = useCallback(
    (root: number | null) => setParams(current ? (root ? { c: String(current), t: String(root) } : { c: String(current) }) : {}),
    [current, setParams],
  );
  const threadRef = useRef(thread);
  threadRef.current = thread;
  const markThread = useCallback((root: number, upTo?: number) => {
    chatApi.threadRead(root, upTo).then(() => window.dispatchEvent(new Event("pos:threads")), () => undefined);
    setMessages((ms) => ms.map((x) => (x.id === root && x.thread ? { ...x, thread: { ...x.thread, unread: 0 } } : x)));
  }, []);
  useEffect(() => {
    setThreadMsgs([]);
    if (!current || !thread) return;
    chatApi.thread(current, thread).then((p) => {
      setThreadMsgs(p.messages);
      markThread(thread);
    }, (e) => setError(e.message));
  }, [current, thread, markThread]);
  const received = useCallback((m: ChatMessage) => {
    setMessages((ms) => (m.reply_to ? applyReply(upsertMsg(ms, m), m, meRef.current, threadRef.current) : upsertMsg(ms, m)));
    const open = threadRef.current;
    if (open && (m.reply_to === open || m.id === open)) setThreadMsgs((ms) => upsertMsg(ms, m));
  }, []);
  const sender = useSender(current, received);

  useEffect(() => {
    if (!current) return;
    chatApi.messages(current).then((p) => {
      setMessages(p.messages);
      setHasMore(p.has_more);
      scrollDown();
      markRead(current);
    }, (e) => setError(e.message));
  }, [current, markRead]);

  const loadOlder = () => {
    if (!current || !messages.length) return;
    const el = listRef.current;
    const h = el?.scrollHeight ?? 0;
    chatApi.messages(current, messages[0].id).then((p) => {
      setMessages((ms) => [...p.messages, ...ms]);
      setHasMore(p.has_more);
      requestAnimationFrame(() => el && el.scrollTo({ top: el.scrollHeight - h }));
    });
  };
  const olderDm = () => {
    if (!current || !messages.length) return Promise.resolve();
    return chatApi.messages(current, messages[0].id).then((p) => {
      setMessages((ms) => [...p.messages, ...ms]);
      setHasMore(p.has_more);
    });
  };

  // Live updates over Server-Sent Events.
  useEffect(() => {
    const es = new EventSource("/api/chat/stream");
    let refresh: ReturnType<typeof setTimeout> | undefined;
    const reloadSoon = () => {
      clearTimeout(refresh);
      refresh = setTimeout(loadChannels, 400);
    };
    const onMsg = (e: MessageEvent) => {
      const ev = JSON.parse(e.data) as StreamEvent;
      if (!("message" in ev)) return;
      if (ev.channel_id === currentRef.current) {
        if (ev.type === "archive") {
          setMessages((ms) => ms.filter((x) => x.id !== ev.message.id));
          setThreadMsgs((ms) => ms.filter((x) => x.id !== ev.message.id));
        } else {
          const el = listRef.current;
          const atBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          received(ev.message);
          if (ev.type === "message") {
            if (atBottom && !ev.message.reply_to) scrollDown();
            markRead(ev.channel_id, ev.message.id);
            if (threadRef.current && ev.message.reply_to === threadRef.current) markThread(threadRef.current, ev.message.id);
          }
        }
      }
      if (ev.type === "message" || ev.type === "archive") reloadSoon();
      if (ev.type === "message" && ev.message.reply_to) window.dispatchEvent(new Event("pos:threads"));
    };
    ["message", "edit", "archive", "reaction"].forEach((x) => es.addEventListener(x, onMsg as EventListener));
    es.addEventListener("channel", reloadSoon);
    es.addEventListener("presence", ((e: MessageEvent) => setPresence(JSON.parse(e.data))) as EventListener);
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    return () => {
      clearTimeout(refresh);
      es.close();
    };
  }, [loadChannels, markRead, received, markThread]);

  // Who is who: active members come from /api/org (archived ones are not in it).
  const orgById = useMemo(() => new Map(org.map((m) => [m.id, m])), [org]);
  const ceoId = members.find((m) => m.is_ceo)?.id ?? org.find((m) => m.role?.toLowerCase() === "ceo" || m.name === "CEO")?.id ?? null;
  const otherOf = useCallback((c: Channel) => c.members.find((m) => m.id !== me) ?? c.members[0], [me]);
  const isArchivedDm = useCallback(
    (c: Channel) => {
      const o = otherOf(c);
      return !!o && org.length > 0 && !o.is_owner && !orgById.has(o.id);
    },
    [otherOf, org.length, orgById],
  );

  const groups = channels.filter((c) => c.kind === "group" && !isSystem(c));
  const systemChannels = channels.filter(isSystem);
  const systemUnread = systemChannels.reduce((n, c) => n + c.unread, 0);
  const allDms = channels.filter((c) => c.kind === "dm" && c.member);
  const archivedDms = allDms.filter(isArchivedDm);
  const dms = allDms.filter((c) => showArchived || !isArchivedDm(c));
  const ceoDm = ceoId ? dms.find((c) => otherOf(c)?.id === ceoId) : undefined;
  // DMs grouped by the other member's team; people first, then teams by name.
  const peopleLabel = t("chat.people");
  const dmGroups: [string, Channel[]][] = (() => {
    const byTeam = new Map<string, Channel[]>();
    for (const c of dms) {
      if (c === ceoDm) continue;
      const o = otherOf(c);
      const key = o?.kind === "human" ? peopleLabel : orgById.get(o?.id ?? -1)?.team ?? t("chat.no_team");
      byTeam.set(key, [...(byTeam.get(key) ?? []), c]);
    }
    return [...byTeam.entries()].sort(([a], [b]) => (a === peopleLabel ? -1 : b === peopleLabel ? 1 : a.localeCompare(b, LOCALE)));
  })();
  const oversight = channels.filter((c) => c.kind === "dm" && !c.member && (showArchived || !isArchivedDm(c)));
  const typingHere = (presence.typing[String(current)] ?? []).filter((e) => e.id !== me);
  const typingIds = new Set(typingHere.map((e) => e.id));
  const workingHere = channel?.members.filter((m) => m.kind !== "human" && working.has(m.id) && !typingIds.has(m.id)) ?? [];
  const rootMsg = thread ? threadMsgs.find((m) => m.id === thread) ?? byId.get(thread) : undefined;
  const threadReplies = thread ? threadMsgs.filter((m) => m.reply_to === thread) : [];
  const isDm = channel?.kind === "dm";
  const dmWith = members.filter((m) => m.id !== me && !allDms.some((c) => c.members.some((x) => x.id === m.id)));
  const memberById = useMemo(() => new Map(members.map((m) => [m.id, m])), [members]);

  const act = (p: Promise<unknown>) => p.catch((e) => setError(e.message));
  const item = (m: ChatMessage, compact = false) => (
    <MessageItem
      key={m.id}
      m={m}
      me={me}
      names={names}
      compact={compact}
      parent={m.reply_to ? byId.get(m.reply_to) : undefined}
      onReply={() => setThread(m.reply_to ?? m.id)}
      onOpenThread={channel?.kind === "dm" ? undefined : () => setThread(m.reply_to ?? m.id)}
      onReact={(e) => act(chatApi.react(m.id, e))}
      onEdit={(b) => act(chatApi.edit(m.id, b))}
      onArchive={() => act(chatApi.archive(m.id))}
    />
  );
  const rail = (c: Channel, pinned = false) => (
    <RailItem key={c.id} c={c} active={c.id === current} working={working} typing={presence.typing[String(c.id)]} pinned={pinned} onClick={() => setParams({ c: String(c.id) })} />
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="hidden md:block">
        <PageHeader kicker={t("chat.kicker")} title={t("nav.chat")} sub={t("chat.sub")} />
      </div>
      {error && (
        <button className="text-left text-xs text-red-400" onClick={() => setError(null)}>
          {error} · {t("act.dismiss")}
        </button>
      )}
      <div className="grid h-[calc(100dvh-190px)] min-h-[420px] grid-cols-1 gap-4 md:h-[calc(100vh-230px)] md:grid-cols-[250px_minmax(0,1fr)] lg:h-[calc(100vh-210px)]">
        {/* Rail: channels and DMs. On a phone it is the list view. */}
        <Panel
          title={t("chat.channels")}
          right={<span className="flex items-center gap-1.5"><WorkingDot on={live} />{live ? t("nav.live") : t("chat.offline")}</span>}
          className={`min-w-0 ${current || threadsView ? "hidden md:flex" : "flex"}`}
          bodyClassName="overflow-y-auto"
        >
          {creating && <NewChannel members={members} me={me} onClose={() => setCreating(false)} onCreated={(c) => { setCreating(false); loadChannels(); setParams({ c: String(c.id) }); }} />}
          <div className="flex flex-col gap-0.5 p-2">
            {(ceoDm || ceoId) && <span className={`${railHeading} pt-1`}>{t("chat.pinned")}</span>}
            {ceoDm ? (
              rail(ceoDm, true)
            ) : ceoId ? (
              <button
                onClick={() => setParams({ dm: String(ceoId) })}
                title={t("chat.ask_ceo_title")}
                className="flex h-[34px] w-full items-center gap-2 rounded px-2.5 text-left text-[13px] font-medium text-ink hover:bg-raised"
              >
                <Pin size={13} className="shrink-0 text-accent" /> {t("chat.ask_ceo")}
              </button>
            ) : null}
            <button
              onClick={() => setParams({ v: "threads" })}
              aria-current={threadsView ? "true" : undefined}
              className={`mt-1 flex h-[34px] w-full items-center gap-2 rounded px-2.5 text-left text-[13px] transition ${threadsView ? "bg-raised text-ink shadow-[inset_2px_0_0_var(--color-accent)]" : (threads?.unread ?? 0) > 0 ? "text-ink hover:bg-raised" : "text-ink-2 hover:bg-raised"}`}
            >
              <MessagesSquare size={14} className="shrink-0 text-ink-2" />
              <span className={(threads?.unread ?? 0) > 0 ? "font-medium" : ""}>{t("m.chat.threads")}</span>
              {(threads?.unread ?? 0) > 0 && <span className="ml-auto rounded-sm bg-accent/15 px-1.5 font-mono text-xs text-accent">{threads!.unread}</span>}
            </button>
            <span className={`${railHeading} ${ceoDm || ceoId ? "" : "pt-1"}`}>
              {t("chat.groups")}
              <button aria-label={t("chat.new_channel")} title={t("chat.new_channel")} onClick={() => setCreating(true)} className="ml-auto text-ink-2 hover:text-accent"><Plus size={14} /></button>
            </span>
            {groups.map((c) => rail(c))}
            {systemChannels.length > 0 && (
              <>
                <button className={`${railHeading} hover:text-ink`} aria-expanded={systemOpen} onClick={() => setSystemOpen((s) => !s)}>
                  <ChevronDown size={13} className={`transition ${systemOpen ? "" : "-rotate-90"}`} />
                  {t("chat.system")}
                  {systemUnread > 0 && <span className="ml-auto rounded-sm bg-raised px-1.5 font-mono text-xs text-ink-2">{systemUnread}</span>}
                </button>
                {(systemOpen || systemChannels.some((c) => c.id === current)) && systemChannels.map((c) => rail(c))}
              </>
            )}
            <span className={railHeading}>{t("chat.dms")}</span>
            {dmGroups.map(([team, list]) => (
              <Fragment key={team}>
                <span className="px-2.5 pt-1.5 pb-0.5 text-xs text-ink-2">{team}</span>
                {list.map((c) => rail(c))}
              </Fragment>
            ))}
            {dmWith.length > 0 && (
              <select
                value=""
                onChange={(e) => e.target.value && setParams({ dm: e.target.value })}
                className="mx-2.5 mt-1 rounded border border-line bg-bg px-1.5 py-1 text-[12px] text-ink-2 outline-none"
                aria-label={t("chat.start_dm")}
              >
                <option value="">+ {t("chat.start_dm")}…</option>
                {dmWith.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
            )}
            <button onClick={() => setShowAll((s) => !s)} className={`${railHeading} hover:text-ink`} title={t("chat.oversight_title")}>
              <Eye size={12} /> {showAll ? t("chat.oversight_hide") : t("chat.oversight_show")}
            </button>
            {showAll && oversight.map((c) => rail(c))}
            {archivedDms.length > 0 && (
              <label className="flex items-center gap-1.5 px-2.5 pt-2 text-xs text-ink-2">
                <input type="checkbox" className="accent-accent" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
                {t("act.show_archived")} ({archivedDms.length})
              </label>
            )}
          </div>
        </Panel>

        {/* Conversation */}
        {threadsView && (
          <Panel className="min-w-0" bodyClassName="overflow-y-auto" title={t("m.chat.threads")} right={threads?.unread ? t("m.chat.unread_n", { n: threads.unread }) : undefined}>
            <button onClick={() => setParams({})} className="flex items-center gap-1 px-4 pt-2 text-xs text-ink-2 md:hidden"><ArrowLeft size={14} /> {t("chat.back")}</button>
            <ThreadList data={threads} onOpen={(it) => setParams({ c: String(it.channel_id), t: String(it.root.id) })} />
          </Panel>
        )}
        <div className={`${threadsView ? "hidden" : current ? "flex" : "hidden md:flex"} min-h-0 min-w-0 gap-4`}>
          <Panel
            className="min-w-0 flex-1"
            bodyClassName="flex min-h-0 flex-col"
            title={channel?.title ?? t("chat.pick")}
            right={channel ? t("chat.members", { n: channel.members.length, vis: t(`chat.vis_short.${channel.visibility}`) }) : undefined}
          >
            {channel && (
              <>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-4 py-2">
                  <button onClick={() => setParams({})} className="text-ink-2 md:hidden" aria-label={t("chat.back")}><ArrowLeft size={16} /></button>
                  {channel.topic && <span className="min-w-0 truncate text-[12px] text-ink-2">{channel.topic}</span>}
                  <span className="flex min-w-0 flex-wrap items-center gap-2 md:ml-auto">
                    {channel.members.slice(0, 8).map((m) => {
                      const w = m.kind !== "human" ? workingOn(memberById.get(m.id) ?? m) : null;
                      return (
                        <span key={m.id} className="flex items-center gap-1 text-xs text-ink-2" title={working.has(m.id) ? t("chat.working_title") : ""}>
                          {m.kind !== "human" && <WorkingDot on={working.has(m.id) || !!w} />}
                          {m.kind !== "human" ? <Link to={`/team/${m.id}`} className="hover:text-ink">{m.name}</Link> : m.name}
                          {w && <WorkingOnText w={w} className="text-ink-2" />}
                        </span>
                      );
                    })}
                    {channel.members.length > 8 && <span className="text-xs text-ink-2">+{channel.members.length - 8}</span>}
                  </span>
                </div>
                {isDm ? (
                  <Messenger
                    mode="dm"
                    viewKey={String(channel.id)}
                    messages={messages}
                    me={channel.member ? me : otherOf(channel)?.id ?? me}
                    names={names}
                    pending={sender.pending}
                    typing={typingHere}
                    hasMore={hasMore}
                    onOlder={olderDm}
                    onRetry={sender.retry}
                    onDiscard={sender.discard}
                    actions={(m) => (
                      <span className="flex items-center rounded border border-line bg-surface">
                        {["👍", "✅", "👀"].map((e) => (
                          <button key={e} aria-label={`${t("chat.react")} ${e}`} onClick={() => act(chatApi.react(m.id, e))} className="px-1.5 py-1 text-[13px] hover:bg-raised">{e}</button>
                        ))}
                      </span>
                    )}
                    top={!channel.member ? <p className="px-4 py-2 text-xs text-ink-2">{t("chat.oversight_note")}</p> : undefined}
                    empty={<p className="px-4 py-6 text-sm text-ink-2">{t("chat.empty")}</p>}
                  />
                ) : (
                  <>
                    <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto py-2">
                      {hasMore && <button onClick={loadOlder} className="mx-auto block py-2 text-xs text-ink-2 hover:text-ink">{t("act.load_more")}</button>}
                      {messages.length === 0 && <p className="px-4 py-6 text-sm text-ink-2">{isSystem(channel) ? t("chat.system_empty") : t("chat.empty")}</p>}
                      {isSystem(channel) ? <NoticeGroups messages={messages} names={names} /> : messages.filter((m) => !m.reply_to).map((m) => item(m))}
                    </div>
                    <TypingLine entries={typingHere.filter((e) => e.thread === null)} />
                  </>
                )}
                {workingHere.length > 0 && !isDm && (
                  <div className="flex items-center gap-2 px-4 pb-1 text-xs text-ink-2" title={t("chat.working_title")}>
                    <WorkingDot on />
                    {t("chat.working_here", { names: workingHere.map((m) => m.name).join(", ") })}
                  </div>
                )}
                {(channel.member || channel.kind === "group") && !isSystem(channel) ? (
                  <Composer
                    channel={channel}
                    members={members}
                    onSent={isDm ? undefined : scrollDown}
                    send={isDm ? (text, priority) => sender.send(text, [], null, priority) : undefined}
                    placeholder={ceoDm && channel.id === ceoDm.id ? t("chat.ceo_placeholder") : undefined}
                  />
                ) : null}
              </>
            )}
            {!channel && <p className="p-6 text-sm text-ink-2">{t("chat.choose")}</p>}
          </Panel>

          {/* Thread: side panel on desktop, full screen on a phone. */}
          {rootMsg && channel && !isDm && (
            <Panel
              title={`${t("m.chat.thread")} · ${replyCount(threadReplies.length)}`}
              right={<button aria-label={t("chat.close_thread")} onClick={() => setThread(null)}><X size={13} /></button>}
              className="fixed inset-2 z-30 lg:static lg:inset-auto lg:w-[380px] lg:shrink-0"
              bodyClassName="flex min-h-0 flex-col"
            >
              {!rootMsg.meeting ? (
                <>
                  <Messenger
                    mode="thread"
                    viewKey={`t${rootMsg.id}`}
                    messages={threadMsgs}
                    root={rootMsg}
                    rootChannel={channel.name ?? undefined}
                    me={me}
                    names={names}
                    pending={sender.pending.filter((p) => p.reply_to === rootMsg.id)}
                    typing={typingHere.filter((e) => e.thread === rootMsg.id)}
                    onRetry={sender.retry}
                    onDiscard={sender.discard}
                  />
                  <Composer
                    channel={channel}
                    members={members}
                    replyTo={rootMsg}
                    send={(text, priority) => sender.send(text, [], rootMsg.id, priority)}
                    placeholder={t("chat.reply_placeholder")}
                  />
                </>
              ) : (
              <>
              <div className="min-h-0 flex-1 overflow-y-auto py-2">
                {item(rootMsg, true)}
                <div className="mx-4 my-1 border-t border-line" />
                {threadReplies.map((m, i) => (
                  <Fragment key={m.id}>
                    <RoundDivider prev={threadReplies[i - 1]} m={m} />
                    {item(m, true)}
                  </Fragment>
                ))}
              </div>
              <TypingLine entries={typingHere.filter((e) => e.thread === rootMsg.id)} />
              <Composer channel={channel} members={members} replyTo={rootMsg} placeholder={t("chat.reply_placeholder")} />
              </>
              )}
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
