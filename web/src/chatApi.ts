import { api } from "./api";

export type Priority = "fyi" | "change_plan" | "stop";
export type Trust = "owner" | "person" | "agent" | "external";

export type ChatMember = {
  id: number;
  name: string;
  kind: "human" | "ai" | "agent";
  is_owner: boolean;
  /** The CEO: the owner's single channel into the company (pinned first in the chat). */
  is_ceo?: boolean;
  role?: "owner" | "member";
  working: boolean;
  /** The task a busy agent's live run works on (the chat's "pracuje na T-046 · 12 min"). */
  current?: { task_id: number | null; task_ref: string | null; title: string | null; since: string; minutes: number } | null;
  remote?: boolean;
  paused?: boolean;
  /** The one "working" source; `current` is the older shape of the same. */
  working_on?: { task_ref: string | null; since: string | null; title?: string | null } | null;
  archived?: boolean;
};

export type Channel = {
  id: number;
  kind: "dm" | "group";
  name: string | null;
  title: string;
  topic: string;
  visibility: "public" | "team" | "private";
  member: boolean;
  members: ChatMember[];
  unread: number;
  mentions: number;
  last_read_message_id: number | null;
  last: { id: number; author_name: string; body: string; created_at: string } | null;
  archived_at: string | null;
  typing?: TypingEntry[];
  /** A DM with an archived member: read-only; messages go to its successor. */
  read_only?: boolean;
  archived_dm?: { archived: string; successor_id: number | null; successor_name: string | null } | null;
};

/** Someone typing in a channel: a person at the composer, or an agent's run working on a reply. */
export type TypingEntry = {
  id: number;
  name: string;
  kind: "human" | "ai" | "agent";
  state: "typing" | "working";
  thread: number | null;
};

export type ChatMessage = {
  id: number;
  channel_id: number;
  author_id: number;
  author_name: string;
  author_kind: "human" | "ai" | "agent";
  body: string;
  reply_to: number | null;
  mentions: number[];
  attachments: { type: string; id: number; ref?: string }[];
  priority: Priority | null;
  trust: Trust;
  reactions: { emoji: string; actors: number[]; count: number }[];
  replies: number;
  created_at: string;
  edited_at: string | null;
  archived_at: string | null;
  /** In a meeting thread (pos.meetings): the agenda, a turn (round, kind) or the decision. */
  meeting?: MeetingMark;
  /** A DM answer quotes the message it answers (a DM has no threads). */
  quote_of?: number | null;
  quote?: Quote;
  /** A thread root in a channel: who answered, the last reply, and what the viewer has not read. */
  thread?: ThreadSummary;
};

export type Quote = { id: number; author_id?: number; author_name: string; body: string };

export type ThreadSummary = {
  repliers: { id: number; name: string; kind: "human" | "ai" | "agent" }[];
  last: { id: number; author_id: number; author_name: string; body: string; created_at: string } | null;
  unread: number;
  last_read_id: number;
};

export type ThreadItem = { root: ChatMessage; channel_id: number; channel_name: string; replies: number; thread: ThreadSummary | null };

export type MeetingMark = {
  meeting: number;
  kind: "agenda" | "position" | "response" | "decision";
  round?: number;
  rounds?: number;
  topic?: string;
  status?: string;
};

export type Page = { channel_id: number; messages: ChatMessage[]; has_more: boolean };

export type StreamEvent =
  | { type: "message" | "edit" | "archive" | "reaction"; channel_id: number; message: ChatMessage }
  | { type: "channel"; channel_id: number };

export type Presence = { working: number[]; typing: Record<string, TypingEntry[]> };

const post = <T,>(path: string, body: unknown = {}) => api<T>(path, { method: "POST", body: JSON.stringify(body) });

export const chatApi = {
  channels: (all = false) => api<Channel[]>(`/api/chat/channels${all ? "?all=true" : ""}`),
  channel: (id: number) => api<Channel>(`/api/chat/channels/${id}`),
  members: () => api<ChatMember[]>("/api/chat/members"),
  createChannel: (name: string, members: number[], topic = "", visibility = "team") =>
    post<Channel>("/api/chat/channels", { name, members, topic, visibility }),
  dm: (to: number) => post<Channel>("/api/chat/dm", { to }),
  messages: (id: number, before?: number) =>
    api<Page>(`/api/chat/channels/${id}/messages?limit=50${before ? `&before=${before}` : ""}`),
  thread: (id: number, root: number) => api<Page>(`/api/chat/channels/${id}/messages?limit=200&thread=${root}`),
  threads: (unread = false) => api<{ threads: ThreadItem[]; unread: number }>(`/api/chat/threads${unread ? "?unread=true" : ""}`),
  threadRead: (root: number, message_id?: number) => post(`/api/chat/threads/${root}/read`, { message_id }),
  send: (id: number, body: string, reply_to?: number | null, priority?: Priority | null, attachments?: { type: string; id: number }[]) =>
    post<ChatMessage>(`/api/chat/channels/${id}/messages`, { body, reply_to, priority, attachments: attachments ?? [] }),
  edit: (mid: number, body: string) => api<ChatMessage>(`/api/chat/messages/${mid}`, { method: "PATCH", body: JSON.stringify({ body }) }),
  archive: (mid: number) => post<ChatMessage>(`/api/chat/messages/${mid}/archive`),
  react: (mid: number, emoji: string) => post<ChatMessage>(`/api/chat/messages/${mid}/react`, { emoji }),
  invite: (id: number, member: number) => post<Channel>(`/api/chat/channels/${id}/members`, { member }),
  read: (id: number, message_id?: number) => post(`/api/chat/channels/${id}/read`, { message_id }),
  typing: (id: number, thread?: number | null) => post(`/api/chat/channels/${id}/typing`, { thread: thread ?? null }),
  typingNow: () => api<Record<string, TypingEntry[]>>("/api/chat/typing"),
};
