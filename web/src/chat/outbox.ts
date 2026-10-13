// The chat's outbox: messages on their way, and drafts, kept per conversation across reloads (localStorage).
// Shared by the desktop chat (pages/Chat.tsx) and the phone app (/m, mobile/Conversation.tsx); pure, so
// `npm test` covers it (tests/outbox.test.ts).
//
// Prod 2026-10-13: the owner wrote to the CEO in their DM and switched to #obseum-ai before the POST answered;
// the answer (the stored message) was put into whatever conversation was open, so his DM message showed in
// #obseum-ai and was gone after a reload. A message is now placed by the channel id the server stored it in
// (`belongsTo`), never by the view that happens to be open, and a send that fails stays visible
// ("Neodesláno – Zkusit znovu") until it is sent or discarded, also after a reload.
import type { Priority } from "../chatApi";

export type OutboxFile = { type: "file"; id: number; name: string; mime?: string | null; preview?: string; size?: number | null;
  version?: number | null; description?: string | null };

/** A message on its way: shown at once, replaced by the real one when the server answers. */
export type Pending = {
  key: string;
  channel_id: number;
  body: string;
  files: OutboxFile[];
  reply_to: number | null;
  priority: Priority | null;
  state: "sending" | "error";
  error?: string;
  created_at: string;
  /** Restored after a reload while it was still on its way: maybe sent, maybe not (reconcile decides). */
  restored?: boolean;
};

/** The subset of Storage the outbox needs (localStorage in the app, a Map in tests). */
export type KV = { getItem(k: string): string | null; setItem(k: string, v: string): void; removeItem(k: string): void };

const OUTBOX = "pos.chat.outbox.";
const DRAFT = "pos.chat.draft.";

export function storage(): KV | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null; // blocked site data: the outbox works for this page load only
  }
}

/** The server's stored message goes where the server put it: only into the conversation it belongs to. */
export const belongsTo = (m: { channel_id: number }, channel: number | null | undefined) => channel != null && m.channel_id === channel;

/** The pending messages of one conversation, as left by an earlier page load. One still "sending" then is not
 * known to have arrived: it comes back as unsent (`restored`) until `reconcile` finds it in the conversation. */
export function loadPending(kv: KV | null, channel: number): Pending[] {
  if (!kv) return [];
  try {
    const raw = kv.getItem(OUTBOX + channel);
    const list = raw ? (JSON.parse(raw) as Pending[]) : [];
    return list
      .filter((p) => p && p.channel_id === channel && typeof p.key === "string")
      .map((p) => (p.state === "sending" ? { ...p, state: "error" as const, restored: true } : p));
  } catch {
    return [];
  }
}

/** Write each conversation's pending messages under its own key (none left: the key goes). */
export function savePending(kv: KV | null, all: Pending[], channels: Iterable<number>) {
  if (!kv) return;
  for (const c of new Set(channels)) {
    const mine = all.filter((p) => p.channel_id === c);
    try {
      if (mine.length) kv.setItem(OUTBOX + c, JSON.stringify(mine));
      else kv.removeItem(OUTBOX + c);
    } catch {
      /* full or blocked: the bubble still shows for this page load */
    }
  }
}

type Sent = { id: number; channel_id: number; author_id: number; body: string; created_at: string; reply_to: number | null };

/** Pending messages that are in the conversation already (sent before a reload, or the live event came before the
 * POST answered) are dropped: the same text, by me, in the same channel and thread, stored no earlier than a minute
 * before it was queued. Only ones whose fate is unknown (on their way, or restored after a reload): a send that
 * failed in this page load stays until it is retried or discarded, even when the same text went out before. */
export function reconcile(pending: Pending[], messages: Sent[], me: number): Pending[] {
  if (!pending.length || !messages.length || !me) return pending;
  const used = new Set<number>();
  return pending.filter((p) => {
    if (p.state === "error" && !p.restored) return true;
    const since = Date.parse(p.created_at) - 60000;
    const hit = messages.find(
      (m) => !used.has(m.id) && m.author_id === me && m.channel_id === p.channel_id && (m.reply_to ?? null) === (p.reply_to ?? null)
        && m.body.trim().startsWith(p.body.trim()) && Date.parse(m.created_at) >= since,
    );
    if (hit) used.add(hit.id);
    return !hit;
  });
}

export const draftKey = (channel: number, thread: number | null = null) => `${DRAFT}${channel}${thread ? `:${thread}` : ""}`;

export function loadDraft(kv: KV | null, channel: number, thread: number | null = null): string {
  try {
    return kv?.getItem(draftKey(channel, thread)) ?? "";
  } catch {
    return "";
  }
}

export function saveDraft(kv: KV | null, channel: number, thread: number | null, text: string) {
  if (!kv) return;
  try {
    if (text.trim()) kv.setItem(draftKey(channel, thread), text);
    else kv.removeItem(draftKey(channel, thread));
  } catch {
    /* ignore */
  }
}
