// Web Push subscribe with one recovery (src/mobile/pushSubscribe.ts): `npm test`.
// Prod 2026-10-13 (T-1431): "subscribe: AbortError: Registration failed - push service error" twice, owner_devices=0.
import assert from "node:assert/strict";
import { test } from "node:test";
import { isPushServiceError, pushServiceMessage, subscribeWithRetry } from "../src/mobile/pushSubscribe.ts";

const abort = () => new DOMException("Registration failed - push service error", "AbortError");
const sub = (endpoint: string, log: string[]) => ({ endpoint, unsubscribe: async () => (log.push(`unsubscribe ${endpoint}`), true) }) as unknown as PushSubscription;
const noPause = async () => undefined;

function manager(results: (Error | string)[], held: string | null, log: string[]) {
  return {
    subscribe: async () => {
      const r = results.shift();
      log.push("subscribe");
      if (r instanceof Error) throw r;
      return sub(r as string, log);
    },
    getSubscription: async () => (held ? sub(held, log) : null),
  };
}

test("a push service error drops the stale subscription and the second try succeeds", async () => {
  const log: string[] = [];
  const { sub: s, retried } = await subscribeWithRetry(manager([abort(), "https://fcm/new"], "https://fcm/old", log), {}, noPause);
  assert.equal(s.endpoint, "https://fcm/new");
  assert.equal(retried, true);
  assert.deepEqual(log, ["subscribe", "unsubscribe https://fcm/old", "subscribe"]);
});

test("a first try that works is not retried", async () => {
  const log: string[] = [];
  const { retried } = await subscribeWithRetry(manager(["https://fcm/a"], null, log), {}, noPause);
  assert.equal(retried, false);
  assert.deepEqual(log, ["subscribe"]);
});

test("a second push service error reaches the caller as a push service error", async () => {
  const log: string[] = [];
  await assert.rejects(subscribeWithRetry(manager([abort(), abort()], null, log), {}, noPause), (e) => isPushServiceError(e));
  assert.deepEqual(log, ["subscribe", "subscribe"]);
});

test("other errors are not retried", async () => {
  const log: string[] = [];
  const denied = new DOMException("Permission denied", "NotAllowedError");
  await assert.rejects(subscribeWithRetry(manager([denied], null, log), {}, noPause), (e) => !isPushServiceError(e));
  assert.deepEqual(log, ["subscribe"]);
});

test("the message tells a Brave user which setting to turn on", () => {
  assert.match(pushServiceMessage(true), /brave:\/\/settings\/privacy/);
  assert.match(pushServiceMessage(false), /push service error/);
});
