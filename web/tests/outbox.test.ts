// The chat outbox (src/chat/outbox.ts), shared by the desktop chat and /m: `npm test`.
// Prod 2026-10-13: the owner's DM message to the CEO showed in #obseum-ai (the view open when the POST answered)
// and was gone after a reload.
import assert from "node:assert/strict";
import { test } from "node:test";
import { type Pending, belongsTo, draftKey, loadDraft, loadPending, reconcile, saveDraft, savePending } from "../src/chat/outbox.ts";

function kv() {
  const m = new Map<string, string>();
  return { m, getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) };
}
const p = (key: string, channel: number, body: string, state: Pending["state"] = "sending", extra: Partial<Pending> = {}): Pending => ({
  key, channel_id: channel, body, files: [], reply_to: null, priority: null, state, created_at: "2026-10-13T11:19:20.000Z", ...extra,
});

test("the stored message belongs to the channel the server put it in, not to the open view", () => {
  const stored = { id: 2964, channel_id: 37 };
  assert.equal(belongsTo(stored, 37), true);
  assert.equal(belongsTo(stored, 109), false); // the owner switched to #obseum-ai before the POST answered
  assert.equal(belongsTo(stored, null), false);
});

test("pending messages are kept per conversation and come back after a reload as unsent", () => {
  const s = kv();
  savePending(s, [p("a", 37, "Jak to vypadá s naší aplikací na tvorbu knihy?"), p("b", 109, "jinde", "error")], [37, 109]);
  assert.ok(s.m.has("pos.chat.outbox.37") && s.m.has("pos.chat.outbox.109"));
  const back = loadPending(s, 37);
  assert.equal(back.length, 1);
  assert.equal(back[0].body, "Jak to vypadá s naší aplikací na tvorbu knihy?");
  assert.equal(back[0].state, "error"); // not known to have arrived: "Neodesláno – Zkusit znovu"
  assert.equal(back[0].restored, true);
  assert.deepEqual(loadPending(s, 109).map((x) => x.key), ["b"]);
  // sent: the conversation's key goes
  savePending(s, [p("b", 109, "jinde", "error")], [37, 109]);
  assert.equal(s.m.has("pos.chat.outbox.37"), false);
  assert.deepEqual(loadPending(s, 37), []);
});

test("a broken or missing store never throws", () => {
  const s = kv();
  s.m.set("pos.chat.outbox.5", "{not json");
  assert.deepEqual(loadPending(s, 5), []);
  assert.deepEqual(loadPending(null, 5), []);
  savePending(null, [p("a", 5, "x")], [5]);
});

test("reconcile drops a restored message the conversation already has, and keeps one that never arrived", () => {
  const ms = [{ id: 2964, channel_id: 37, author_id: 1, body: "Jak to vypadá?", created_at: "2026-10-13T11:19:21+00:00", reply_to: null }];
  const left = reconcile([p("a", 37, "Jak to vypadá?", "error", { restored: true }), p("b", 37, "Druhá zpráva", "error", { restored: true })], ms, 1);
  assert.deepEqual(left.map((x) => x.key), ["b"]);
  // someone else's message with the same text, another thread, or an older one: not mine
  assert.equal(reconcile([p("a", 37, "Jak to vypadá?")], [{ ...ms[0], author_id: 19 }], 1).length, 1);
  assert.equal(reconcile([p("a", 37, "Jak to vypadá?")], [{ ...ms[0], reply_to: 5 }], 1).length, 1);
  assert.equal(reconcile([p("a", 37, "Jak to vypadá?")], [{ ...ms[0], created_at: "2026-10-12T11:19:21+00:00" }], 1).length, 1);
  // a send that failed in this page load is never dropped, even when the same text went out a moment before
  assert.equal(reconcile([p("a", 37, "Jak to vypadá?", "error")], ms, 1).length, 1);
  // two equal pending, one stored: one stays
  assert.equal(reconcile([p("a", 37, "ok?"), p("b", 37, "ok?")], [{ ...ms[0], body: "ok?" }], 1).length, 1);
});

test("drafts are kept per conversation and thread", () => {
  const s = kv();
  saveDraft(s, 37, null, "rozepsáno");
  saveDraft(s, 67, 2965, "do vlákna");
  assert.equal(loadDraft(s, 37), "rozepsáno");
  assert.equal(loadDraft(s, 109), "");
  assert.equal(loadDraft(s, 67, 2965), "do vlákna");
  assert.equal(loadDraft(s, 67), "");
  saveDraft(s, 37, null, "  ");
  assert.equal(s.m.has(draftKey(37)), false);
});
