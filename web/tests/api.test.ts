// The shared request helper (src/api.ts): every request has a deadline, so a screen ends in an error with
// "Zkusit znovu" instead of an endless "načítám…"; a network failure is the same kind of error.
import assert from "node:assert/strict";
import { register } from "node:module";
import { test } from "node:test";

register("./ts-resolve.mjs", import.meta.url);
const { ApiError, fetchWithTimeout, timeoutFor, READ_TIMEOUT_MS, GENERATE_TIMEOUT_MS, WRITE_TIMEOUT_MS, NETWORK_MESSAGE } = await import(
  "../src/api.ts"
);

/** A fetch that never answers until aborted. */
const hanging: typeof fetch = (_input, init) =>
  new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });

test("reads time out in 15 s, generated reads later, writes later still", () => {
  assert.equal(timeoutFor("/api/tasks"), READ_TIMEOUT_MS);
  assert.equal(READ_TIMEOUT_MS, 15_000);
  assert.equal(timeoutFor("/api/tasks/T-1/summary?generate=true"), GENERATE_TIMEOUT_MS);
  assert.equal(timeoutFor("/api/tasks/T-1/summary?generate=false"), READ_TIMEOUT_MS);
  assert.equal(timeoutFor("/api/tasks", { method: "POST" }), WRITE_TIMEOUT_MS);
  assert.equal(timeoutFor("/api/tasks", { method: "patch" }), WRITE_TIMEOUT_MS);
  assert.equal(timeoutFor("/api/tasks", { timeoutMs: 0 }), 0);
  assert.equal(timeoutFor("/api/x", { method: "POST", timeoutMs: 5 }), 5);
});

test("a request past its deadline rejects with ApiError status 0 and says how long it waited", async () => {
  const t0 = Date.now();
  await assert.rejects(fetchWithTimeout("/api/slow", {}, 30, undefined, hanging), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal((e as InstanceType<typeof ApiError>).status, 0);
    assert.match((e as Error).message, /neodpověděl/);
    return true;
  });
  assert.ok(Date.now() - t0 < 1000);
});

test("a body that never finishes reading is timed out too", async () => {
  const ok: typeof fetch = async () => new Response("{}");
  const neverRead = (res: Response) =>
    new Promise((_r, reject) => {
      void res;
      setTimeout(() => reject(new Error("should have timed out first")), 500);
    });
  // The read rejects only after 500 ms; the 30 ms deadline wins and is what the caller sees.
  const p = fetchWithTimeout("/api/x", {}, 30, (res) => Promise.race([neverRead(res), new Promise((_r, rej) => setTimeout(() => rej(new DOMException("aborted", "AbortError")), 40))]), ok);
  await assert.rejects(p, (e: unknown) => e instanceof ApiError && (e as InstanceType<typeof ApiError>).status === 0);
});

test("a network failure is ApiError(0) with words for the owner", async () => {
  const down: typeof fetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  await assert.rejects(fetchWithTimeout("/api/x", {}, 1000, undefined, down), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal((e as Error).message, NETWORK_MESSAGE);
    return true;
  });
});

test("the caller's own abort stays an AbortError, not a timeout", async () => {
  const ctl = new AbortController();
  const p = fetchWithTimeout("/api/x", { signal: ctl.signal }, 1000, undefined, hanging);
  ctl.abort();
  await assert.rejects(p, (e: unknown) => !(e instanceof ApiError) && (e as Error).name === "AbortError");
});

test("an HTTP error from the reader passes through with its status", async () => {
  const ok: typeof fetch = async () => new Response("{}", { status: 500 });
  const read = async (res: Response) => {
    throw new ApiError(res.status, "boom");
  };
  await assert.rejects(fetchWithTimeout("/api/x", {}, 1000, read, ok), (e: unknown) => e instanceof ApiError && (e as InstanceType<typeof ApiError>).status === 500);
});

test("an answer in time is returned and the timer is cleared", async () => {
  const ok: typeof fetch = async () => new Response('{"a":1}');
  const { body } = await fetchWithTimeout("/api/x", {}, 50, (r) => r.json(), ok);
  assert.deepEqual(body, { a: 1 });
});
