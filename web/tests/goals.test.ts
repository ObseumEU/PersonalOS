// The goal form's request body: only what is filled in (new) or changed (edit). Run: npm test.
import assert from "node:assert/strict";
import { test } from "node:test";
import { formOf, goalBody, parseNumber } from "../src/goalsForm.ts";

test("numbers take a Czech decimal comma; empty is null; words are NaN", () => {
  assert.equal(parseNumber("12,5"), 12.5);
  assert.equal(parseNumber(" 1 200 "), 1200);
  assert.equal(parseNumber(""), null);
  assert.ok(Number.isNaN(parseNumber("hodně")));
});

test("a new goal sends only the filled fields", () => {
  const f = { ...formOf(), title: " Odpovědi do 24 h ", metric: "medián hodin", baseline: "28,2", target_value: "24" };
  assert.deepEqual(goalBody(f), { title: "Odpovědi do 24 h", metric: "medián hodin", baseline: 28.2, target_value: 24 });
});

test("an edit sends what changed, an emptied field as null", () => {
  const before = formOf({ title: "A", why: "", target: "", owner_name: "CEO", due: "2026-12-31", status: "active", progress: 40, parent_id: null, current: 3 });
  assert.deepEqual(goalBody(before, before), {});
  const after = { ...before, due: "", progress: "", current: "5", status: "paused" };
  assert.deepEqual(goalBody(after, before), { due: null, status: "paused", progress: null, current: 5 });
});

test("a bad number is named instead of sent", () => {
  assert.deepEqual(goalBody({ ...formOf(), title: "A", progress: "150" }), { invalid: "progress" });
  assert.deepEqual(goalBody({ ...formOf(), title: "A", current: "x" }), { invalid: "current" });
});
