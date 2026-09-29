// The messenger timeline (src/chat/timeline.ts): `npm test` (node --test, types stripped).
import assert from "node:assert/strict";
import { test } from "node:test";
import { applyReply, channelTimeline, dmTimeline, rows, threadTimeline, upsert } from "../src/chat/timeline.ts";

const OWNER = 1;
const CEO = 2;
let seq = 0;
const at = (min: number) => new Date(Date.UTC(2026, 8, 29, 8, 0) + min * 60000).toISOString();
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function msg(id: number, author: number, body: string, min: number, extra: Record<string, unknown> = {}): any {
  seq++;
  return {
    id, channel_id: 5, author_id: author, author_name: author === OWNER ? "David" : "CEO", author_kind: author === OWNER ? "human" : "agent",
    body, reply_to: null, mentions: [], attachments: [], priority: null, trust: "owner", reactions: [], replies: 0,
    created_at: at(min), edited_at: null, archived_at: null, ...extra,
  };
}
const words = { today: "Dnes", yesterday: "Včera", locale: "cs-CZ" };

test("a DM flattens the old thread replies into one timeline, in order", () => {
  const q1 = msg(10, OWNER, "Jak jsme na tom s knihou?", 0, { replies: 2 });
  const q2 = msg(12, OWNER, "A obálka?", 2);
  const a1 = msg(11, CEO, "Kapitola 3 je hotová.", 1, { reply_to: 10 }); // right after its question
  const a1b = msg(13, CEO, "A korektura běží.", 3, { reply_to: 10 }); // after q2: needs a quote
  const a2 = msg(14, CEO, "Obálka v pátek.", 4, { quote_of: 12 }); // the new flat answer, after a1b
  const items = dmTimeline([q2, a1b, q1, a2, a1]);
  assert.deepEqual(items.map((i) => i.m.id), [10, 11, 12, 13, 14]);
  assert.equal(items[1].quote, undefined);
  assert.equal(items[3].quote?.id, 10);
  assert.match(items[3].quote?.body ?? "", /^Jak jsme/);
  assert.equal(items[4].quote?.id, 12);
});

test("a second answer to the same message right after the first needs no quote", () => {
  const items = dmTimeline([msg(1, OWNER, "Stav?", 0), msg(2, CEO, "Koukám.", 1, { reply_to: 1 }), msg(3, CEO, "Vše běží.", 2, { reply_to: 1 })]);
  assert.deepEqual(items.map((i) => !!i.quote), [false, false, false]);
});

test("a quote of a message not loaded comes from the server", () => {
  const items = dmTimeline([msg(50, CEO, "Ano.", 0, { quote_of: 3, quote: { id: 3, author_name: "David", body: "Starší otázka" } })]);
  assert.equal(items[0].quote?.body, "Starší otázka");
});

test("channels keep threads: top level only; a thread is its replies", () => {
  const all = [msg(1, OWNER, "root", 0, { replies: 2 }), msg(2, CEO, "r1", 1, { reply_to: 1 }), msg(3, OWNER, "next", 2), msg(4, CEO, "r2", 3, { reply_to: 1 })];
  assert.deepEqual(channelTimeline(all).map((i) => i.m.id), [1, 3]);
  assert.deepEqual(threadTimeline(all, 1).map((i) => i.m.id), [2, 4]);
});

test("rows: day separators and groups of one author within five minutes", () => {
  const now = new Date(Date.UTC(2026, 8, 29, 12, 0));
  const y = (min: number) => new Date(Date.UTC(2026, 8, 28, 9, min)).toISOString();
  const items = dmTimeline([
    msg(1, OWNER, "a", 0, { created_at: y(0) }),
    msg(2, OWNER, "b", 0, { created_at: y(1) }),
    msg(3, CEO, "c", 0, { created_at: y(2) }),
    msg(4, CEO, "d", 30),
    msg(5, CEO, "e", 31),
    msg(6, CEO, "f", 60),
  ]);
  const r = rows(items, OWNER, now, words);
  assert.deepEqual(r.map((x) => (x.kind === "day" ? x.label : `${x.item.m.body}${x.first ? "^" : ""}${x.last ? "$" : ""}${x.mine ? "<" : ""}`)), [
    "Včera", "a^<", "b$<", "c^$", "Dnes", "d^", "e$", "f^$",
  ]);
});

test("a live reply updates its root's chip; unread unless open or mine", () => {
  const root = msg(1, OWNER, "root", 0, { replies: 1, thread: { repliers: [{ id: CEO, name: "CEO", kind: "agent" }], last: null, unread: 0, last_read_id: 0 } });
  let ms = applyReply([root], msg(2, 7, "od QA", 1, { reply_to: 1, author_name: "QA" }), OWNER, null);
  assert.equal(ms[0].replies, 2);
  assert.equal(ms[0].thread.unread, 1);
  assert.deepEqual(ms[0].thread.repliers.map((r: { name: string }) => r.name), ["QA", "CEO"]);
  assert.equal(ms[0].thread.last.body, "od QA");
  ms = applyReply(ms, msg(3, OWNER, "moje", 2, { reply_to: 1 }), OWNER, null);
  assert.equal(ms[0].thread.unread, 1);
  ms = applyReply(ms, msg(4, CEO, "otevřené", 3, { reply_to: 1 }), OWNER, 1);
  assert.equal(ms[0].thread.unread, 1);
  assert.equal(applyReply(ms, msg(4, CEO, "otevřené", 3, { reply_to: 1 }), OWNER, null)[0].replies, 4); // counted once
});

test("upsert keeps order and a known chip", () => {
  const a = msg(2, OWNER, "b", 1, { thread: { repliers: [], last: null, unread: 3, last_read_id: 0 } });
  const out = upsert(upsert([a], msg(1, OWNER, "a", 0)), msg(2, OWNER, "b edited", 1));
  assert.deepEqual(out.map((m) => m.id), [1, 2]);
  assert.equal(out[1].thread.unread, 3);
  assert.ok(seq > 0);
});
