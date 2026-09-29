/*
 * The messenger timeline, as plain data (no React: tests run it with node, see tests/timeline.test.ts).
 *
 * A DM is one flat conversation: every message in order, thread replies included (agents
 * used to answer DMs in threads, which the phone hid). A reply that does not follow the message
 * it answers carries a small quote of it. A channel shows its top-level messages (threads
 * stay under a chip); a thread shows its root and its replies.
 */
import type { ChatMessage, Quote } from "../chatApi";

export type Item = { m: ChatMessage; quote?: Quote };

export type Row =
  | { kind: "day"; key: string; label: string }
  | { kind: "msg"; key: string; item: Item; mine: boolean; first: boolean; last: boolean };

/** Messages this close together from one author form one group (one name, one avatar). */
export const GROUP_MS = 5 * 60 * 1000;

export function snippet(body: string, n = 120): string {
  const text = body.replace(/\s+/g, " ").trim();
  return text.length > n ? `${text.slice(0, n)}…` : text;
}

const byOrder = (a: ChatMessage, b: ChatMessage) => a.id - b.id;

/** What a message answers: the DM quote (quote_of), else the thread it was posted in (reply_to). */
export const answers = (m: ChatMessage): number | null => m.quote_of ?? m.reply_to ?? null;

/** A DM, flattened: everything in order; a quote where the answer is not right after its question. */
export function dmTimeline(messages: ChatMessage[]): Item[] {
  const sorted = [...messages].sort(byOrder);
  const byId = new Map(sorted.map((m) => [m.id, m]));
  return sorted.map((m, i) => {
    const target = answers(m);
    if (!target) return { m };
    const prev = sorted[i - 1];
    // Right after what it answers, or a second answer to the same message: no quote needed.
    if (prev && (prev.id === target || (answers(prev) === target && prev.author_id === m.author_id))) return { m };
    const src = byId.get(target);
    const quote: Quote | undefined = src ? { id: src.id, author_id: src.author_id, author_name: src.author_name, body: snippet(src.body) } : m.quote;
    return quote ? { m, quote } : { m };
  });
}

/** A channel: its top-level messages (replies live in their threads). */
export function channelTimeline(messages: ChatMessage[]): Item[] {
  return [...messages].sort(byOrder).filter((m) => !m.reply_to).map((m) => ({ m }));
}

/** A thread: its replies in order (the root is shown pinned above them). */
export function threadTimeline(messages: ChatMessage[], root: number): Item[] {
  return [...messages].sort(byOrder).filter((m) => m.reply_to === root).map((m) => ({ m }));
}

const dayKey = (d: Date) => `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;

export type DayWords = { today: string; yesterday: string; locale: string };

export function dayLabel(iso: string, now: Date, words: DayWords): string {
  const d = new Date(iso);
  if (dayKey(d) === dayKey(now)) return words.today;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (dayKey(d) === dayKey(y)) return words.yesterday;
  const days = (now.getTime() - d.getTime()) / 864e5;
  if (days < 6) return d.toLocaleDateString(words.locale, { weekday: "long" });
  return d.toLocaleDateString(words.locale, { day: "numeric", month: "long", ...(d.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) });
}

/** The rows to draw: day separators, and each message marked as the first / last of its group. */
export function rows(items: Item[], me: number, now: Date, words: DayWords): Row[] {
  const out: Row[] = [];
  let lastDay = "";
  let prev: Item | undefined;
  for (const item of items) {
    const d = new Date(item.m.created_at);
    const day = dayKey(d);
    if (day !== lastDay) {
      out.push({ kind: "day", key: `day-${day}`, label: dayLabel(item.m.created_at, now, words) });
      lastDay = day;
      prev = undefined;
    }
    const joins =
      !!prev &&
      prev.m.author_id === item.m.author_id &&
      !item.quote &&
      d.getTime() - new Date(prev.m.created_at).getTime() < GROUP_MS;
    if (joins) {
      const before = out[out.length - 1];
      if (before.kind === "msg") before.last = false;
    }
    out.push({ kind: "msg", key: `m-${item.m.id}`, item, mine: item.m.author_id === me, first: !joins, last: true });
    prev = item;
  }
  return out;
}

/** A reply arrived live: its root's chip shows it (count, last reply, who, unread unless it is open or mine). */
export function applyReply(messages: ChatMessage[], reply: ChatMessage, me: number, open: number | null): ChatMessage[] {
  const root = reply.reply_to;
  if (!root) return messages;
  return messages.map((x) => {
    if (x.id !== root) return x;
    const t = x.thread ?? { repliers: [], last: null, unread: 0, last_read_id: 0 };
    if (t.last && t.last.id >= reply.id) return x; // already counted
    const who = { id: reply.author_id, name: reply.author_name, kind: reply.author_kind };
    return {
      ...x,
      replies: x.replies + 1,
      thread: {
        ...t,
        last: { id: reply.id, author_id: reply.author_id, author_name: reply.author_name, body: snippet(reply.body, 140), created_at: reply.created_at },
        repliers: [who, ...t.repliers.filter((r) => r.id !== reply.author_id)].slice(0, 3),
        unread: reply.author_id === me || open === root ? t.unread : t.unread + 1,
      },
    };
  });
}

/** Insert or replace a message (live events and the answer to a send may bring the same one). */
export function upsert(messages: ChatMessage[], m: ChatMessage): ChatMessage[] {
  return messages.some((x) => x.id === m.id) ? messages.map((x) => (x.id === m.id ? { ...m, thread: m.thread ?? x.thread } : x)) : [...messages, m].sort(byOrder);
}

/** "právě teď", "před 5 min", "před 3 h", then the day. */
export function ago(iso: string, now: Date, words: DayWords & { now: string; min: string; hours: string }): string {
  const s = (now.getTime() - new Date(iso).getTime()) / 1000;
  if (s < 60) return words.now;
  if (s < 3600) return words.min.replace("{n}", String(Math.floor(s / 60)));
  if (s < 6 * 3600) return words.hours.replace("{n}", String(Math.floor(s / 3600)));
  const label = dayLabel(iso, now, words);
  return label === words.today ? new Date(iso).toLocaleTimeString(words.locale, { hour: "2-digit", minute: "2-digit" }) : label;
}
