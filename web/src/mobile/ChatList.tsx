import { Bell, ChevronDown, Hash, Pin, Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { type Channel, type ChatMember, chatApi } from "../chatApi";
import { t } from "../i18n/core";
import ThreadList, { useThreads } from "../chat/ThreadList";
import { stripToolMarkup } from "../toolMarkup";
import { isMessageEvent, onChatEvent, usePresence } from "./live";
import { ActionSheet, Avatar, Dots, SheetButton, TopBar, when } from "./ui";

const isSystem = (c: Channel) => c.kind === "group" && c.name === "system";

/** Loaded once and shared: who is who (the CEO, agents' current work). */
let membersCache: ChatMember[] = [];
export function useMembers(): ChatMember[] {
  const [m, setM] = useState(membersCache);
  useEffect(() => {
    const load = () =>
      chatApi.members().then((x) => {
        membersCache = x;
        setM(x);
      }, () => undefined);
    load();
    const h = setInterval(() => document.visibilityState === "visible" && load(), 30000);
    return () => clearInterval(h);
  }, []);
  return m;
}

/** What a busy member works on: "pracuje na T-12", else "pracuje…". */
export function workLabel(m: ChatMember | undefined, working: boolean): string | null {
  const ref = m?.working_on?.task_ref ?? m?.current?.task_ref ?? null;
  if (ref) return t("m.chat.working_on", { ref });
  return working ? t("m.chat.working") : null;
}

function Row({ c, me, members, typing, working, pinned }: { c: Channel; me: number; members: Map<number, ChatMember>; typing: boolean; working: Set<number>; pinned?: boolean }) {
  const other = c.kind === "dm" ? c.members.find((m) => m.id !== me) ?? c.members[0] : undefined;
  const who = other ? members.get(other.id) ?? other : undefined;
  const busy = !!other && (working.has(other.id) || !!who?.working_on);
  const state = typing ? t("m.chat.typing") : other && other.kind !== "human" ? workLabel(who, busy) : null;
  const title = c.kind === "group" ? `#${c.name}` : c.title;
  const last = c.last ? `${c.last.author_name === (members.get(me)?.name ?? "") ? "Ty: " : c.kind === "group" ? `${c.last.author_name}: ` : ""}${stripToolMarkup(c.last.body).replace(/\s+/g, " ")}` : "";
  return (
    <Link to={`/m/chat/${c.id}`} className="flex min-h-[68px] items-center gap-3 px-4 py-2 active:bg-raised">
      <span className="relative">
        {c.kind === "group" ? (
          <span className="grid h-11 w-11 place-items-center rounded-full border border-line bg-raised text-ink-2">
            {isSystem(c) ? <Bell size={18} /> : <Hash size={18} />}
          </span>
        ) : (
          <Avatar name={title} size={44} human={other?.kind === "human"} />
        )}
        {busy && <span className="sonar absolute -right-0.5 -bottom-0.5 h-3 w-3 rounded-full border-2 border-bg bg-accent" aria-hidden />}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex items-baseline gap-2">
          <span className={`truncate text-[16px] ${c.unread ? "font-medium text-ink" : "text-ink"}`}>{title}</span>
          {pinned && <Pin size={13} className="shrink-0 text-accent" aria-label={t("chat.pinned")} />}
          {c.last && <span className={`ml-auto shrink-0 text-[12px] ${c.unread ? "text-accent" : "text-ink-2"}`}>{when(c.last.created_at)}</span>}
        </span>
        <span className="flex items-center gap-2">
          {state ? (
            <span className="flex min-w-0 items-center gap-1.5 truncate text-[13px] text-accent">
              {typing && <Dots />}
              {state}
            </span>
          ) : (
            <span className={`truncate text-[13px] ${c.unread ? "text-ink" : "text-ink-2"}`}>{last || (pinned ? t("m.chat.ceo_hint") : "")}</span>
          )}
          {c.unread > 0 && (
            <span className={`ml-auto shrink-0 rounded-full px-2 py-0.5 font-mono text-[12px] leading-4 ${c.mentions ? "bg-amber-300 text-bg" : "bg-accent text-bg"}`}>{c.unread}</span>
          )}
        </span>
      </span>
    </Link>
  );
}

// Threads change when a reply comes in (anywhere) or a thread is read.
const onReply = (reload: () => void) => onChatEvent((ev) => isMessageEvent(ev) && !!ev.message.reply_to && reload());

export default function ChatList() {
  const [channels, setChannels] = useState<Channel[] | null>(null);
  const [tab, setTab] = useState<"all" | "threads">(() => (sessionStorage.getItem("pos:m-chat-tab") === "threads" ? "threads" : "all"));
  const threads = useThreads(onReply);
  const pick = (v: "all" | "threads") => {
    setTab(v);
    try {
      sessionStorage.setItem("pos:m-chat-tab", v);
    } catch {
      /* private mode: the tab is not remembered */
    }
  };
  const [systemOpen, setSystemOpen] = useState(false);
  const [picking, setPicking] = useState(false);
  const members = useMembers();
  const presence = usePresence();
  const navigate = useNavigate();
  const load = useCallback(() => chatApi.channels().then(setChannels, () => undefined), []);

  useEffect(() => {
    load();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const soon = () => {
      clearTimeout(timer);
      timer = setTimeout(load, 300);
    };
    const off = onChatEvent((ev) => (ev.type === "message" || ev.type === "archive" || ev.type === "channel") && soon());
    window.addEventListener("pos:resume", load);
    return () => {
      off();
      clearTimeout(timer);
      window.removeEventListener("pos:resume", load);
    };
  }, [load]);

  const me = members.find((m) => m.is_owner)?.id ?? 0;
  const byId = useMemo(() => new Map(members.map((m) => [m.id, m])), [members]);
  const working = useMemo(() => new Set(presence.working), [presence]);
  const ceo = members.find((m) => m.is_ceo);
  const list = channels ?? [];
  const mine = list.filter((c) => c.member || c.kind === "group");
  const ceoDm = ceo ? mine.find((c) => c.kind === "dm" && c.members.some((m) => m.id === ceo.id)) : undefined;
  const recent = (c: Channel) => c.last?.created_at ?? "";
  const dms = mine.filter((c) => c.kind === "dm" && c !== ceoDm).sort((a, b) => recent(b).localeCompare(recent(a)));
  const groups = mine.filter((c) => c.kind === "group" && !isSystem(c)).sort((a, b) => recent(b).localeCompare(recent(a)));
  const system = mine.filter(isSystem);
  const typingIn = (c: Channel) => (presence.typing[String(c.id)] ?? []).some((e) => e.id !== me);
  const rowProps = { me, members: byId, working };
  const noDm = members.filter((m) => m.id !== me && !m.archived && !mine.some((c) => c.kind === "dm" && c.members.some((x) => x.id === m.id)));

  const openDm = (id: number) =>
    chatApi.dm(id).then((c) => {
      setPicking(false);
      navigate(`/m/chat/${c.id}`);
    });

  return (
    <div className="flex flex-col">
      <TopBar
        title={t("m.chat.title")}
        right={
          <button aria-label={t("m.chat.new_dm")} onClick={() => setPicking(true)} className="grid h-11 w-11 place-items-center rounded-full text-ink-2 active:bg-raised">
            <Plus size={20} />
          </button>
        }
      />
      <div role="tablist" aria-label={t("m.chat.title")} className="flex gap-2 px-4 pt-2 pb-1">
        {(["all", "threads"] as const).map((v) => (
          <button
            key={v}
            role="tab"
            aria-selected={tab === v}
            onClick={() => pick(v)}
            className={`flex h-9 items-center gap-1.5 rounded-full border px-4 text-[14px] ${tab === v ? "border-accent bg-accent/15 text-accent" : "border-line text-ink-2"}`}
          >
            {v === "all" ? t("m.chat.all") : t("m.chat.threads")}
            {v === "threads" && (threads?.unread ?? 0) > 0 && (
              <span className="rounded-full bg-accent px-1.5 font-mono text-[11px] leading-[18px] text-bg">{threads!.unread}</span>
            )}
          </button>
        ))}
      </div>
      {tab === "threads" ? (
        <ThreadList data={threads} onOpen={(it) => navigate(`/m/chat/${it.channel_id}?thread=${it.root.id}`)} />
      ) : (
      <>
      {channels === null && <p className="px-4 py-6 text-sm text-ink-2">{t("act.loading")}</p>}
      {channels !== null && mine.length === 0 && <p className="px-4 py-6 text-sm text-ink-2">{t("m.chat.empty_list")}</p>}
      {ceoDm ? (
        <Row c={ceoDm} {...rowProps} typing={typingIn(ceoDm)} pinned />
      ) : ceo ? (
        <button onClick={() => openDm(ceo.id)} className="flex min-h-[68px] items-center gap-3 px-4 text-left active:bg-raised">
          <Avatar name="CEO" size={44} />
          <span className="flex flex-col">
            <span className="text-[16px]">{t("chat.ask_ceo")}</span>
            <span className="text-[13px] text-ink-2">{t("m.chat.ceo_hint")}</span>
          </span>
        </button>
      ) : null}
      {dms.length > 0 && <h2 className="px-4 pt-4 pb-1 text-xs font-medium text-ink-2">{t("m.chat.agents")}</h2>}
      {dms.map((c) => <Row key={c.id} c={c} {...rowProps} typing={typingIn(c)} />)}
      {groups.length > 0 && <h2 className="px-4 pt-4 pb-1 text-xs font-medium text-ink-2">{t("m.chat.channels")}</h2>}
      {groups.map((c) => <Row key={c.id} c={c} {...rowProps} typing={typingIn(c)} />)}
      {system.length > 0 && (
        <>
          <button onClick={() => setSystemOpen((s) => !s)} aria-expanded={systemOpen} className="flex h-11 items-center gap-2 px-4 pt-2 text-xs font-medium text-ink-2">
            <ChevronDown size={14} className={`transition ${systemOpen ? "" : "-rotate-90"}`} />
            {t("m.chat.system")}
            {system.reduce((n, c) => n + c.unread, 0) > 0 && <span className="ml-auto font-mono">{system.reduce((n, c) => n + c.unread, 0)}</span>}
          </button>
          {systemOpen && system.map((c) => <Row key={c.id} c={c} {...rowProps} typing={false} />)}
        </>
      )}
      </>
      )}
      {picking && (
        <ActionSheet title={t("m.chat.new_dm")} onClose={() => setPicking(false)}>
          <p className="px-3 pb-1 text-xs text-ink-2">{t("m.chat.new_dm")}</p>
          <div className="max-h-[60dvh] overflow-y-auto">
            {noDm.length === 0 && <p className="px-3 py-3 text-sm text-ink-2">—</p>}
            {noDm.map((m) => (
              <SheetButton key={m.id} icon={<Avatar name={m.name} size={28} human={m.kind === "human"} />} onClick={() => openDm(m.id)}>
                {m.name}
              </SheetButton>
            ))}
          </div>
        </ActionSheet>
      )}
    </div>
  );
}
