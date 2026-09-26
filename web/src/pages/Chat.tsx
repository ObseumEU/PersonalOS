import { Archive, ArrowLeft, AtSign, Eye, Hash, MessageSquare, Pencil, Plus, Send, SmilePlus, X } from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { type Channel, type ChatMember, type ChatMessage, type Presence, type Priority, type StreamEvent, type TypingEntry, chatApi } from "../chatApi";
import { PageHeader, Panel } from "../components/ui";

const EMOJI = ["👍", "✅", "👀", "🎉", "❤️", "🙏"];
const PRIORITY_CLS: Record<Priority, string> = {
  fyi: "border-line text-ink-2!",
  change_plan: "border-accent/60 text-accent!",
  stop: "border-amber-400/70 text-amber-300!",
};

function hhmm(iso: string) {
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Light markdown: ```code```, `code`, **bold**, links, @mentions and T-123 task links. */
function Body({ text, names }: { text: string; names: string[] }) {
  const inline = useMemo(() => {
    const mention = names.length ? `@(?:${[...names].sort((a, b) => b.length - a.length).map(escapeRe).join("|")})` : "@\\w+";
    return new RegExp(`(\`[^\`\\n]+\`|\\*\\*[^*\\n]+\\*\\*|https?://[^\\s<]+|\\bT-\\d{1,6}\\b|${mention})`, "gi");
  }, [names]);
  const renderInline = (s: string, key: string): ReactNode[] =>
    s.split(inline).map((part, i) => {
      const k = `${key}-${i}`;
      if (i % 2 === 0) return <Fragment key={k}>{part}</Fragment>;
      if (part.startsWith("`")) return <code key={k} className="rounded-[3px] bg-raised px-1 font-mono text-[12px]">{part.slice(1, -1)}</code>;
      if (part.startsWith("**")) return <strong key={k} className="font-medium">{part.slice(2, -2)}</strong>;
      if (part.startsWith("http")) return <a key={k} href={part} target="_blank" rel="noreferrer noopener" className="text-accent underline decoration-accent/40">{part}</a>;
      if (/^T-\d+$/i.test(part)) return <Link key={k} to={`/tasks?task=${part.toUpperCase()}`} className="rounded-[3px] bg-accent/10 px-1 font-mono text-[12px] text-accent">{part.toUpperCase()}</Link>;
      return <span key={k} className="rounded-[3px] bg-accent/15 px-0.5 text-accent">{part}</span>;
    });
  const blocks = text.split(/```(?:\w+\n)?/);
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
  if (m.trust === "external") return <span className="cap rounded-[3px] border border-amber-400/50 px-1 text-[9px]! text-amber-300!" title="Arrived over A2A from outside PersonalOS: agents see it as untrusted data">A2A · EXTERNAL</span>;
  if (m.trust === "agent") return <span className="cap rounded-[3px] border border-line px-1 text-[9px]!" title="From an agent: other agents see it wrapped as data, not orders">AGENT</span>;
  return null;
}

/** "pracuje na T-046 · 12 min", linking to the task. */
function CurrentWork({ c }: { c: NonNullable<ChatMember["current"]> }) {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(c.since).getTime()) / 60000));
  return (
    <span className="normal-case text-ink-3" title={c.title ?? undefined}>
      {" · pracuje na "}
      {c.task_ref ? <Link to={`/tasks?task=${c.task_ref}`} className="text-accent hover:underline">{c.task_ref}</Link> : "běhu"}
      {` · ${minutes} min`}
    </span>
  );
}

function WorkingDot({ on }: { on: boolean }) {
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${on ? "sonar bg-accent" : "bg-dim"}`} />;
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

/** Czech: "Hlídač píše", "Hlídač a Asistent vedení píšou", "3 lidé píšou", "5 lidí píše". */
function who(list: TypingEntry[], one: string, few: string, many: string) {
  const n = list.length;
  if (n === 1) return `${list[0].name} ${one}`;
  if (n === 2) return `${list[0].name} a ${list[1].name} ${few}`;
  return n <= 4 ? `${n} lidé ${few}` : `${n} lidí ${many}`;
}

function typingLabel(entries: TypingEntry[]) {
  const typing = entries.filter((e) => e.state === "typing");
  const working = entries.filter((e) => e.state === "working");
  return [
    typing.length ? `${who(typing, "píše", "píšou", "píše")}…` : "",
    working.length ? `${who(working, "pracuje na tom", "pracují na tom", "pracuje na tom")}…` : "",
  ].filter(Boolean).join(" · ");
}

/** Under a message list: who is typing (or working on a reply). Always takes its line, so the list does not jump. */
function TypingLine({ entries }: { entries: TypingEntry[] }) {
  const label = typingLabel(entries);
  const soft = entries.length > 0 && entries.every((e) => e.state === "working");
  return (
    <div className="flex h-5 items-center gap-2 px-4 text-[12px] text-ink-3" aria-live="polite" role="status">
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
    <div tabIndex={0} className={`group relative flex flex-col gap-1 px-4 py-2 outline-none hover:bg-raised/50 focus-within:bg-raised/50 ${mentioned ? "shadow-[inset_2px_0_0_var(--color-accent)]" : ""}`}>
      {parent && !compact && (
        <button onClick={onOpenThread} className="cap flex min-w-0 items-center gap-1 text-left hover:text-ink-2!">
          ↳ <span className="shrink-0 whitespace-nowrap text-ink-2">{parent.author_name}</span>
          <span className="truncate">{parent.body.slice(0, 90)}</span>
        </button>
      )}
      <div className="flex flex-wrap items-baseline gap-2">
        <span className={`text-[13px] font-medium ${m.author_kind === "human" ? "text-ink" : "text-accent"}`}>{m.author_name}</span>
        <KindTag m={m} />
        {m.priority && <span className={`cap rounded-[3px] border px-1 text-[9px]! ${PRIORITY_CLS[m.priority]}`}>{m.priority.replace("_", " ").toUpperCase()}</span>}
        <span className="cap text-[10px]!">{hhmm(m.created_at)}{m.edited_at ? " · edited" : ""}</span>
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
          <textarea autoFocus rows={2} value={editing} onChange={(e) => setEditing(e.target.value)} className="rounded border border-line bg-bg p-2 text-[13px] outline-none focus:border-accent" />
          <span className="flex gap-2">
            <button className="btn-accent">Save</button>
            <button type="button" className="btn" onClick={() => setEditing(null)}>Cancel</button>
          </span>
        </form>
      ) : (
        <Body text={m.body} names={names} />
      )}
      {(m.reactions.length > 0 || (m.replies > 0 && !compact)) && (
        <div className="flex flex-wrap items-center gap-1.5">
          {m.reactions.map((r) => (
            <button
              key={r.emoji}
              onClick={() => onReact(r.emoji)}
              className={`flex h-6 items-center gap-1 rounded-full border px-2 text-[12px] ${r.actors.includes(me) ? "border-accent/60 bg-accent/10" : "border-line"}`}
            >
              {r.emoji} <span className="font-mono text-[11px] text-ink-2">{r.count}</span>
            </button>
          ))}
          {m.replies > 0 && !compact && (
            <button onClick={onOpenThread} className="cap flex items-center gap-1 text-accent!">
              <MessageSquare size={12} /> {m.replies} {m.replies === 1 ? "reply" : "replies"}
            </button>
          )}
        </div>
      )}
      <div className="absolute top-1 right-3 hidden items-center gap-0.5 rounded border border-line bg-surface p-0.5 group-hover:flex group-focus-within:flex">
        <button aria-label="Reply in thread" title="Reply in thread" onClick={onReply} className="p-1 text-ink-3 hover:text-accent"><MessageSquare size={14} /></button>
        <button aria-label="React" title="React" onClick={() => setPicking((p) => !p)} className="p-1 text-ink-3 hover:text-accent"><SmilePlus size={14} /></button>
        {mine && <button aria-label="Edit" title="Edit (history is kept)" onClick={() => setEditing(m.body)} className="p-1 text-ink-3 hover:text-accent"><Pencil size={14} /></button>}
        <button aria-label="Archive" title="Archive (never deleted)" onClick={() => window.confirm("Archive this message? It stays in the history.") && onArchive()} className="p-1 text-ink-3 hover:text-amber-300"><Archive size={14} /></button>
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

function Composer({
  channel, members, replyTo, onCancelReply, onSent, placeholder,
}: {
  channel: Channel;
  members: ChatMember[];
  replyTo?: ChatMessage | null;
  onCancelReply?: () => void;
  onSent?: () => void;
  placeholder?: string;
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
        <span className="cap flex min-w-0 items-center gap-2">
          <span className="shrink-0 whitespace-nowrap">↳ REPLYING TO {replyTo.author_name.toUpperCase()}</span>
          <span className="truncate text-ink-2">{replyTo.body.slice(0, 60)}</span>
          {onCancelReply && <button aria-label="Cancel reply" onClick={onCancelReply} className="ml-auto text-ink-3 hover:text-ink"><X size={13} /></button>}
        </span>
      )}
      {candidates.length > 0 && (
        <div role="listbox" className="absolute bottom-full left-3 z-20 mb-1 w-64 rounded border border-line bg-surface py-1 shadow-lg">
          {candidates.map((m, i) => (
            <button
              key={m.id}
              role="option"
              aria-selected={i === pick}
              onMouseDown={(e) => { e.preventDefault(); complete(m); }}
              className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] ${i === pick ? "bg-raised text-ink" : "text-ink-2"}`}
            >
              <WorkingDot on={m.working} />
              {m.name}
              <span className="cap ml-auto">{m.is_owner ? "owner" : m.kind === "human" ? "person" : m.remote ? "remote" : "agent"}</span>
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
          placeholder={placeholder ?? `Message ${channel.title} · @ to mention · T-123 links a task`}
          className="min-w-0 flex-1 resize-none rounded border border-line bg-bg p-2 text-[14px] outline-none focus:border-accent"
        />
        <button className="btn-accent h-[38px]!" onClick={submit} aria-label="Send">
          <Send size={14} />
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {hasAgents && (
          <label className="cap flex items-center gap-1.5" title="Priority for agents: fyi arrives at their next step, change_plan interrupts a running session, stop pauses whom it names">
            PRIORITY
            <select value={priority} onChange={(e) => setPriority(e.target.value as Priority | "")} className="rounded border border-line bg-bg px-1 py-0.5 font-mono text-[11px] text-ink-2 outline-none">
              <option value="">none</option>
              <option value="fyi">fyi</option>
              <option value="change_plan">change_plan</option>
              <option value="stop">stop</option>
            </select>
          </label>
        )}
        <span className="cap ml-auto hidden sm:inline">ENTER SENDS · SHIFT+ENTER NEW LINE</span>
      </div>
      {error && <span className="cap text-red-400!">{error}</span>}
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
      <span className="cap flex items-center">NEW CHANNEL <button type="button" aria-label="Close" onClick={onClose} className="ml-auto"><X size={13} /></button></span>
      <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="release-friday" className="rounded border border-line bg-bg px-2 py-1 text-[13px] outline-none focus:border-accent" />
      <input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="Topic (optional)" className="rounded border border-line bg-bg px-2 py-1 text-[13px] outline-none focus:border-accent" />
      <div className="flex max-h-32 flex-col overflow-y-auto">
        {members.filter((m) => m.id !== me).map((m) => (
          <label key={m.id} className="flex items-center gap-2 py-0.5 text-[13px] text-ink-2">
            <input type="checkbox" className="accent-accent" checked={picked.includes(m.id)} onChange={(e) => setPicked((p) => (e.target.checked ? [...p, m.id] : p.filter((x) => x !== m.id)))} />
            {m.name}
          </label>
        ))}
      </div>
      <select value={visibility} onChange={(e) => setVisibility(e.target.value)} className="rounded border border-line bg-bg px-2 py-1 text-[12px] text-ink-2 outline-none">
        <option value="team">team: every member may read and join</option>
        <option value="private">private: invite only</option>
        <option value="public">public</option>
      </select>
      <button className="btn-accent justify-center" disabled={!name.trim()}>Create</button>
      {error && <span className="cap text-red-400!">{error}</span>}
    </form>
  );
}

function RailItem({ c, active, working, typing, onClick }: { c: Channel; active: boolean; working: Set<number>; typing?: TypingEntry[]; onClick: () => void }) {
  const others = c.members.filter((m) => !m.is_owner);
  const busy = c.kind === "dm" && others.some((m) => working.has(m.id));
  return (
    <button
      onClick={onClick}
      className={`flex h-[34px] w-full items-center gap-2 rounded px-2.5 text-left text-[13px] transition ${
        active ? "bg-raised text-ink shadow-[inset_2px_0_0_var(--color-accent)]" : c.unread ? "text-ink hover:bg-raised" : "text-ink-2 hover:bg-raised"
      }`}
    >
      {c.kind === "group" ? <Hash size={14} className="shrink-0 text-ink-3" /> : <WorkingDot on={busy} />}
      <span className={`truncate ${c.unread ? "font-medium" : ""}`}>{c.kind === "group" ? c.name : c.title}</span>
      {typing && typing.length > 0 && (
        <span className="shrink-0 text-accent" title={typingLabel(typing)} aria-label={typingLabel(typing)}><TypingDots /></span>
      )}
      {c.mentions > 0 && <AtSign size={12} className="shrink-0 text-accent" />}
      {c.unread > 0 && <span className="cap ml-auto rounded-sm bg-accent/15 px-1.5 text-accent!">{c.unread}</span>}
    </button>
  );
}

export default function Chat() {
  const [params, setParams] = useSearchParams();
  const current = Number(params.get("c")) || null;
  const [channels, setChannels] = useState<Channel[]>([]);
  const [members, setMembers] = useState<ChatMember[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [presence, setPresence] = useState<Presence>({ working: [], typing: {} });
  const [thread, setThread] = useState<number | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [creating, setCreating] = useState(false);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const currentRef = useRef(current);
  currentRef.current = current;

  const me = members.find((m) => m.is_owner)?.id ?? 0;
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

  // Default to #team on desktop when nothing is selected.
  useEffect(() => {
    if (!current && !params.get("dm") && channels.length && window.matchMedia("(min-width: 768px)").matches) {
      const team = channels.find((c) => c.name === "team") ?? channels[0];
      setParams({ c: String(team.id) }, { replace: true });
    }
  }, [current, channels, params, setParams]);

  const scrollDown = () => requestAnimationFrame(() => listRef.current?.scrollTo({ top: listRef.current.scrollHeight }));
  const markRead = useCallback((id: number, upTo?: number) => {
    chatApi.read(id, upTo).then(() => setChannels((cs) => cs.map((c) => (c.id === id ? { ...c, unread: 0, mentions: 0 } : c))), () => undefined);
  }, []);

  useEffect(() => {
    setThread(null);
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

  // Live updates over Server-Sent Events.
  useEffect(() => {
    const es = new EventSource("/api/chat/stream");
    let refresh: ReturnType<typeof setTimeout> | undefined;
    const reloadSoon = () => {
      clearTimeout(refresh);
      refresh = setTimeout(loadChannels, 400);
    };
    const upsert = (m: ChatMessage) =>
      setMessages((ms) => {
        let next = ms.some((x) => x.id === m.id) ? ms.map((x) => (x.id === m.id ? m : x)) : [...ms, m];
        if (m.reply_to) next = next.map((x) => (x.id === m.reply_to && !ms.some((y) => y.id === m.id) ? { ...x, replies: x.replies + 1 } : x));
        return next;
      });
    const onMsg = (e: MessageEvent) => {
      const ev = JSON.parse(e.data) as StreamEvent;
      if (!("message" in ev)) return;
      if (ev.channel_id === currentRef.current) {
        if (ev.type === "archive") setMessages((ms) => ms.filter((x) => x.id !== ev.message.id));
        else {
          const el = listRef.current;
          const atBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          upsert(ev.message);
          if (ev.type === "message") {
            if (atBottom) scrollDown();
            markRead(ev.channel_id, ev.message.id);
          }
        }
      }
      if (ev.type === "message" || ev.type === "archive") reloadSoon();
    };
    ["message", "edit", "archive", "reaction"].forEach((t) => es.addEventListener(t, onMsg as EventListener));
    es.addEventListener("channel", reloadSoon);
    es.addEventListener("presence", ((e: MessageEvent) => setPresence(JSON.parse(e.data))) as EventListener);
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    return () => {
      clearTimeout(refresh);
      es.close();
    };
  }, [loadChannels, markRead]);

  const groups = channels.filter((c) => c.kind === "group");
  const dms = channels.filter((c) => c.kind === "dm" && c.member);
  const oversight = channels.filter((c) => c.kind === "dm" && !c.member);
  const typingHere = (presence.typing[String(current)] ?? []).filter((e) => e.id !== me);
  const typingIds = new Set(typingHere.map((e) => e.id));
  const workingHere = channel?.members.filter((m) => m.kind !== "human" && working.has(m.id) && !typingIds.has(m.id)) ?? [];
  const rootMsg = thread ? byId.get(thread) : undefined;
  const threadReplies = thread ? messages.filter((m) => m.reply_to === thread) : [];
  const dmWith = members.filter((m) => m.id !== me && !dms.some((c) => c.members.some((x) => x.id === m.id)));

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
      onOpenThread={() => setThread(m.reply_to ?? m.id)}
      onReact={(e) => act(chatApi.react(m.id, e))}
      onEdit={(b) => act(chatApi.edit(m.id, b))}
      onArchive={() => act(chatApi.archive(m.id))}
    />
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="hidden md:block">
        <PageHeader kicker="LAB · CHAT" title="Chat" sub="People and agents, in channels and DMs. A2A stays for systems outside PersonalOS." />
      </div>
      {error && (
        <button className="cap text-left text-red-400!" onClick={() => setError(null)}>
          {error} · dismiss
        </button>
      )}
      <div className="grid h-[calc(100dvh-190px)] min-h-[420px] grid-cols-1 gap-4 md:h-[calc(100vh-230px)] md:grid-cols-[240px_1fr] lg:h-[calc(100vh-210px)]">
        {/* Rail: channels and DMs. On a phone it is the list view. */}
        <Panel
          fig="CH"
          title="Channels"
          right={<span className="flex items-center gap-1.5"><WorkingDot on={live} />{live ? "live" : "offline"}</span>}
          className={current ? "hidden md:flex" : "flex"}
          bodyClassName="overflow-y-auto"
        >
          {creating && <NewChannel members={members} me={me} onClose={() => setCreating(false)} onCreated={(c) => { setCreating(false); loadChannels(); setParams({ c: String(c.id) }); }} />}
          <div className="flex flex-col gap-0.5 p-2">
            <span className="cap flex items-center px-2.5 pt-1 pb-1">
              GROUPS
              <button aria-label="New channel" title="New channel" onClick={() => setCreating(true)} className="ml-auto text-ink-3 hover:text-accent"><Plus size={13} /></button>
            </span>
            {groups.map((c) => <RailItem key={c.id} c={c} active={c.id === current} working={working} typing={presence.typing[String(c.id)]} onClick={() => setParams({ c: String(c.id) })} />)}
            <span className="cap px-2.5 pt-3 pb-1">DIRECT MESSAGES</span>
            {dms.map((c) => <RailItem key={c.id} c={c} active={c.id === current} working={working} typing={presence.typing[String(c.id)]} onClick={() => setParams({ c: String(c.id) })} />)}
            {dmWith.length > 0 && (
              <select
                value=""
                onChange={(e) => e.target.value && setParams({ dm: e.target.value })}
                className="mx-2.5 mt-1 rounded border border-line bg-bg px-1.5 py-1 text-[12px] text-ink-3 outline-none"
                aria-label="Start a DM"
              >
                <option value="">+ message someone…</option>
                {dmWith.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
            )}
            <button onClick={() => setShowAll((s) => !s)} className="cap flex items-center gap-1.5 px-2.5 pt-3 pb-1 hover:text-ink-2!" title="Read-only view of DMs between other members">
              <Eye size={12} /> {showAll ? "HIDE" : "SHOW"} AGENTS' DMS
            </button>
            {showAll && oversight.map((c) => <RailItem key={c.id} c={c} active={c.id === current} working={working} typing={presence.typing[String(c.id)]} onClick={() => setParams({ c: String(c.id) })} />)}
          </div>
        </Panel>

        {/* Conversation */}
        <div className={`${current ? "flex" : "hidden md:flex"} min-h-0 min-w-0 gap-4`}>
          <Panel
            className="min-w-0 flex-1"
            bodyClassName="flex min-h-0 flex-col"
            title={channel?.title ?? "Pick a channel"}
            fig={channel ? (channel.kind === "group" ? "#" : "DM") : undefined}
            right={channel ? `${channel.members.length} members · ${channel.visibility}` : undefined}
          >
            {channel && (
              <>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-4 py-2">
                  <button onClick={() => setParams({})} className="text-ink-3 md:hidden" aria-label="Back to channels"><ArrowLeft size={16} /></button>
                  {channel.topic && <span className="truncate text-[12px] text-ink-2">{channel.topic}</span>}
                  <span className="ml-auto flex flex-wrap items-center gap-2">
                    {channel.members.slice(0, 8).map((m) => (
                      <span key={m.id} className="cap flex items-center gap-1" title={working.has(m.id) ? "working: has a running run" : ""}>
                        {m.kind !== "human" && <WorkingDot on={working.has(m.id)} />}
                        {m.kind !== "human" ? <Link to={`/agents/${m.id}`} className="hover:text-ink-2!">{m.name}</Link> : m.name}
                        {m.kind !== "human" && m.current && <CurrentWork c={m.current} />}
                      </span>
                    ))}
                    {channel.members.length > 8 && <span className="cap">+{channel.members.length - 8}</span>}
                  </span>
                </div>
                <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto py-2">
                  {hasMore && <button onClick={loadOlder} className="cap mx-auto block py-2 hover:text-ink-2!">LOAD OLDER</button>}
                  {!channel.member && <p className="cap px-4 py-2">You are reading this DM as the owner; it belongs to its two members.</p>}
                  {messages.length === 0 && <p className="cap px-4 py-6">No messages yet. Say hello, or @mention an agent to bring it in.</p>}
                  {messages.map((m) => item(m))}
                </div>
                <TypingLine entries={typingHere} />
                {workingHere.length > 0 && (
                  <div className="cap flex items-center gap-2 px-4 pb-1" title="Has a running run (not necessarily about this chat)">
                    <WorkingDot on />
                    {`${workingHere.map((m) => m.name).join(", ")} working…`}
                  </div>
                )}
                {channel.member || channel.kind === "group" ? (
                  <Composer channel={channel} members={members} onSent={scrollDown} />
                ) : null}
              </>
            )}
            {!channel && <p className="cap p-6">Choose a channel or a DM on the left.</p>}
          </Panel>

          {/* Thread: side panel on desktop, full screen on a phone. */}
          {rootMsg && channel && (
            <Panel
              fig="THREAD"
              title={`${threadReplies.length} ${threadReplies.length === 1 ? "reply" : "replies"}`}
              right={<button aria-label="Close thread" onClick={() => setThread(null)}><X size={13} /></button>}
              className="fixed inset-2 z-30 lg:static lg:inset-auto lg:w-[340px] lg:shrink-0"
              bodyClassName="flex min-h-0 flex-col"
            >
              <div className="min-h-0 flex-1 overflow-y-auto py-2">
                {item(rootMsg, true)}
                <div className="mx-4 my-1 border-t border-line" />
                {threadReplies.map((m) => item(m, true))}
              </div>
              <TypingLine entries={typingHere.filter((e) => e.thread === rootMsg.id)} />
              <Composer
                channel={channel}
                members={members}
                replyTo={rootMsg}
                placeholder="Reply in thread…"
              />
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
