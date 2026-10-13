// Where the answer to the owner's message stands, typing states, and replies shown under his message
// (src/chat/answers.ts), shared by the desktop chat and /m: `npm test`.
import assert from "node:assert/strict";
import { test } from "node:test";
import { type AnswerStatus, animated, answerFor, answerLabel, inlineReplies, threadReplies, typingKey } from "../src/chat/answers.ts";

const st = (extra: Partial<AnswerStatus>): AnswerStatus => ({
  message_id: 2964, channel_id: 37, thread: null, since: "2026-10-13T11:19:21+00:00", agent_id: 19, agent: "CEO", task: "T-1398",
  state: "queued", until: null, ...extra,
});
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const msg = (id: number, author: number, extra: Record<string, unknown> = {}): any => ({
  id, channel_id: 67, author_id: author, author_name: author === 1 ? "Owner" : "Kniha Lead", author_kind: author === 1 ? "human" : "agent",
  body: `m${id}`, reply_to: null, created_at: "2026-10-13T11:20:54+00:00", archived_at: null, ...extra,
});

test("each answer state has its line", () => {
  assert.deepEqual(answerLabel(st({})), { key: "chat.answer.queued", params: { agent: "CEO" } });
  assert.equal(answerLabel(st({ state: "working" })).key, "chat.answer.working");
  assert.equal(answerLabel(st({ state: "held" })).key, "chat.answer.held");
  const held = answerLabel(st({ state: "held", until: "2026-10-13T14:05:00+00:00" }), "cs-CZ");
  assert.equal(held.key, "chat.answer.held_until");
  assert.match(String(held.params.time), /\d{1,2}:\d{2}/);
  assert.equal(answerLabel(st({ state: "unclaimed", agent: null, agent_id: null })).key, "chat.answer.unclaimed");
  assert.equal(answerLabel(st({ state: "answered" })).key, "chat.answer.answered");
});

test("typing states: writing and preparing move, queued and held are still", () => {
  assert.equal(typingKey({ state: "typing" }), "m.chat.typing");
  assert.equal(typingKey({ state: "working" }), "m.chat.working_reply");
  assert.equal(typingKey({ state: "queued" }), "m.chat.queued");
  assert.equal(typingKey({ state: "held" }), "m.chat.held");
  assert.equal(animated({ state: "typing" }), true);
  assert.equal(animated({ state: "working" }), true);
  assert.equal(animated({ state: "queued" }), false);
  assert.equal(animated({ state: "held" }), false);
});

test("the status shows in its own conversation and thread only", () => {
  const answers = { "37": st({}), "67": st({ channel_id: 67, message_id: 2970, thread: 2965 }) };
  assert.equal(answerFor(answers, 37)?.message_id, 2964);
  assert.equal(answerFor(answers, 109), null); // never in #obseum-ai
  assert.equal(answerFor(answers, 67, 2965)?.message_id, 2970);
  assert.equal(answerFor(answers, 67, 2999), null);
  assert.equal(answerFor(undefined, 37), null);
});

test("an agent's thread reply to the owner's message shows under it in the channel", () => {
  const root = msg(2965, 1);
  const all = [root, msg(2967, 36, { reply_to: 2965 }), msg(2968, 36, { reply_to: 2965 }), msg(2969, 1, { reply_to: 2965 }), msg(2970, 36, { reply_to: 2000 })];
  assert.deepEqual(inlineReplies(all, root, 1).map((m) => m.id), [2967, 2968]);
  assert.deepEqual(inlineReplies(all, root, 1, 1).map((m) => m.id), [2968]);
  assert.deepEqual(inlineReplies(all, msg(2000, 36), 1), []); // not his message
  assert.deepEqual(inlineReplies([...all, msg(2971, 36, { reply_to: 2965, archived_at: "x" })], root, 1).map((m) => m.id), [2967, 2968]);
});

test("a thread's replies come from its own load and from the channel page, once each", () => {
  // prod 2026-10-13: the thread load failed (500) and the panel said "0 odpovědí" while the card said 1
  const channel = [msg(2965, 1), msg(2967, 36, { reply_to: 2965 })];
  assert.deepEqual(threadReplies([], channel, 2965).map((m) => m.id), [2967]);
  const loaded = [msg(2965, 1), msg(2967, 36, { reply_to: 2965, body: "newer" }), msg(2968, 36, { reply_to: 2965 })];
  const merged = threadReplies(loaded, channel, 2965);
  assert.deepEqual(merged.map((m) => m.id), [2967, 2968]);
  assert.equal(merged[0].body, "newer");
});
