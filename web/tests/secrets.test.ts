// Tajemství (src/secretsApi.ts): the order the owner reads the secrets in, and the search. The page never has a
// value to show: the API returns none (backend/tests/test_secrets.py).
import assert from "node:assert/strict";
import { register } from "node:module";
import { test } from "node:test";

register("./ts-resolve.mjs", import.meta.url);
const { matchSecret, sortSecrets } = await import("../src/secretsApi.ts");

type S = Parameters<typeof matchSecret>[0];
const s = (name: string, extra: Partial<S> = {}): S => ({
  name, description: "", purpose: "", origin: "agent", creator: "Kniha Dev", creator_id: 1, team: "kniha",
  project: null, sensitivity: "normal", hosts: [], fields: [], credentials: [name], holders: [], last_use: null,
  uses_7d: 0, pending: [], created_at: "2026-10-13T08:00:00+00:00", rotated_at: null, stored: true, ...extra,
});

test("high sensitivity first, then a pending share, then by name", () => {
  const out = sortSecrets([
    s("b-normal"),
    s("a-normal"),
    s("z-high", { sensitivity: "high" }),
    s("c-pending", { pending: [{ approval_id: 1, from: "A", to: ["B"], why: null, at: "" }] }),
  ]).map((x) => x.name);
  assert.deepEqual(out, ["z-high", "c-pending", "a-normal", "b-normal"]);
});

test("search looks at the name, people, team and hosts, case-insensitively", () => {
  const x = s("kniha-cms", { hosts: ["cms.kniha.example"], holders: [{ agent_id: 2, name: "Ops Bot", granted_by: [], since: "" }] });
  assert.ok(matchSecret(x, "CMS"));
  assert.ok(matchSecret(x, "ops bot"));
  assert.ok(matchSecret(x, "kniha"));
  assert.ok(matchSecret(x, "  "));
  assert.ok(!matchSecret(x, "stripe"));
});
