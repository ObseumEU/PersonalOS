// Where the answer to the owner's message stands (pos.answer_status, sent with the chat presence) and who
// "types": shared by the desktop chat and /m; pure, so `npm test` covers it (tests/answers.test.ts).
// The owner wrote to the CEO and to #kniha and saw "nikdo nepíše" while the answer was queued or being
// worked on (prod 2026-10-13); now the conversation says it: queued, writing, waiting for budget, or nobody
// took it (with "Připomenout").
import type { ChatMessage, TypingEntry } from "../chatApi";

export type AnswerState = "queued" | "working" | "held" | "unclaimed" | "answered";

export type AnswerStatus = {
  message_id: number;
  channel_id: number;
  thread: number | null;
  since: string;
  agent_id: number | null;
  agent: string | null;
  task: string | null;
  state: AnswerState;
  until: string | null;
};

/** A dictionary key and its parameters (the component calls t()). */
export type Label = { key: string; params: Record<string, string | number> };

const hm = (iso: string, locale: string) => new Date(iso).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });

/** The line under the owner's message. */
export function answerLabel(st: AnswerStatus, locale = "cs-CZ"): Label {
  const agent = st.agent ?? "";
  switch (st.state) {
    case "queued":
      return { key: "chat.answer.queued", params: { agent } };
    case "working":
      return { key: "chat.answer.working", params: { agent } };
    case "held":
      return st.until ? { key: "chat.answer.held_until", params: { agent, time: hm(st.until, locale) } } : { key: "chat.answer.held", params: { agent } };
    case "answered":
      return { key: "chat.answer.answered", params: { agent } };
    default:
      return { key: "chat.answer.unclaimed", params: {} };
  }
}

/** A typing entry's verb: "píše…" / "připravuje odpověď…" / "ve frontě" / "čeká na rozpočet". */
export function typingKey(e: Pick<TypingEntry, "state">): string {
  return e.state === "typing" ? "m.chat.typing" : e.state === "working" ? "m.chat.working_reply" : e.state === "held" ? "m.chat.held" : "m.chat.queued";
}

/** Dots move while someone is at it (typing or a run working); queued and held are still. */
export const animated = (e: Pick<TypingEntry, "state">) => e.state === "typing" || e.state === "working";

/** The status for this conversation's view: in a thread only its own message, in the channel only a top-level one. */
export function answerFor(answers: Record<string, AnswerStatus> | undefined, channel: number | null, thread: number | null = null): AnswerStatus | null {
  const st = channel != null ? answers?.[String(channel)] : undefined;
  if (!st) return null;
  if (thread != null) return st.thread === thread || st.message_id === thread ? st : null;
  return st;
}

/** Agents' replies in a thread under the owner's own message: shown under it in the channel too, since the owner
 * expects the answer under his message (prod 2026-10-13, #kniha: the link was only in a thread he could not open). */
export function inlineReplies(messages: ChatMessage[], root: ChatMessage, me: number, max = 2): ChatMessage[] {
  if (root.author_id !== me || root.reply_to) return [];
  return messages
    .filter((m) => m.reply_to === root.id && m.author_id !== me && m.author_kind !== "human" && !m.archived_at)
    .sort((a, b) => a.id - b.id)
    .slice(-max);
}

/** A thread's replies: the ones loaded for the thread and the ones the channel page already has, once each. */
export function threadReplies(threadMsgs: ChatMessage[], channelMsgs: ChatMessage[], root: number): ChatMessage[] {
  const byId = new Map<number, ChatMessage>();
  for (const m of [...channelMsgs, ...threadMsgs]) if (m.reply_to === root && !m.archived_at) byId.set(m.id, m);
  return [...byId.values()].sort((a, b) => a.id - b.id);
}
