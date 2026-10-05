// @mention suggestions in the chat composers (desktop and /m). Run: npm test.
import assert from "node:assert/strict";
import { test } from "node:test";
import { completeMention, mentionCandidates, mentionQuery } from "../src/chat/mentions.ts";

const members = [
  { id: 1, name: "David" },
  { id: 2, name: "CEO" },
  { id: 3, name: "Customer Success" },
  { id: 4, name: "Účetní" },
];

test("the query is what follows @ right before the caret", () => {
  assert.equal(mentionQuery("ahoj @Cu"), "Cu");
  assert.equal(mentionQuery("@"), "");
  assert.equal(mentionQuery("mail@firma"), null);
  assert.equal(mentionQuery("@CEO hotovo"), null);
  assert.equal(mentionQuery("@Cu a dál", 3), "Cu");
});

test("members of the channel come first; a DM suggests only its members", () => {
  const group = { kind: "group", members: [{ id: 3 }] };
  assert.deepEqual(mentionCandidates("c", members, group).map((m) => m.id), [3, 2]);
  assert.deepEqual(mentionCandidates("", members, { kind: "dm", members: [{ id: 1 }, { id: 2 }] }).map((m) => m.id), [1, 2]);
  assert.deepEqual(mentionCandidates(null, members, group), []);
  assert.equal(mentionCandidates("", members, group, 2).length, 2);
});

test("completing puts the whole name and a space in, the caret after it", () => {
  assert.deepEqual(completeMention("ahoj @Cu", 8, "Customer Success"), { value: "ahoj @Customer Success ", caret: 23 });
  assert.deepEqual(completeMention("@C, díky", 2, "CEO"), { value: "@CEO , díky", caret: 5 });
});
