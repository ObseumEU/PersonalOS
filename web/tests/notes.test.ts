// Bringing a note back from the archive posts no body; a body means "restore this version". Run: npm test.
import assert from "node:assert/strict";
import { register } from "node:module";
import { test } from "node:test";

register("./ts-resolve.mjs", import.meta.url);
const { notesApi } = await import("../src/filesApi.ts");

type Sent = { url: string; init: RequestInit };

function capture(): Sent[] {
  const sent: Sent[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    sent.push({ url, init });
    return new Response(JSON.stringify({ id: 7 }), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  return sent;
}

test("unarchiving a note posts to /restore with no body", async () => {
  const sent = capture();
  await notesApi.unarchive(7);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, "/api/notes/7/restore");
  assert.equal(sent[0].init.method, "POST");
  assert.equal(sent[0].init.body, undefined);
});

test("restoring a version sends that version", async () => {
  const sent = capture();
  await notesApi.restore(7, 3);
  assert.equal(sent[0].url, "/api/notes/7/restore");
  assert.deepEqual(JSON.parse(String(sent[0].init.body)), { version: 3 });
});
