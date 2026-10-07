// The owner's input in an agent's live browser (src/handoffApi.ts): which keys go as keys and which as text,
// where a click lands on the agent's page, and the time left. The server validates the same shapes
// (backend/tests/test_handoff.py, pos.handoff.clean_event).
import assert from "node:assert/strict";
import { register } from "node:module";
import { test } from "node:test";

register("./ts-resolve.mjs", import.meta.url);
const { isOpen, isPending, keyName, minutesLeft, pointAt } = await import("../src/handoffApi.ts");

const k = (key: string, mods: Partial<{ ctrlKey: boolean; altKey: boolean; metaKey: boolean; shiftKey: boolean }> = {}) =>
  keyName({ key, ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, ...mods });

test("printable characters are typed text, not keys", () => {
  assert.equal(k("a"), null);
  assert.equal(k("Ř", { shiftKey: true }), null);
  assert.equal(k("@"), null);
});

test("named keys and shortcuts get Playwright's names", () => {
  assert.equal(k("Enter"), "Enter");
  assert.equal(k("Tab", { shiftKey: true }), "Shift+Tab");
  assert.equal(k("a", { ctrlKey: true }), "Control+a");
  assert.equal(k("V", { metaKey: true, shiftKey: true }), "Meta+v");
  assert.equal(k(" "), "Space");
  assert.equal(k("Esc"), "Escape");
});

test("modifier keys alone send nothing", () => {
  for (const m of ["Shift", "Control", "Alt", "Meta", "CapsLock", "Dead"]) assert.equal(k(m), "");
});

test("a point on the picture maps to 0..1 of the agent's viewport, clamped", () => {
  const r = { left: 100, top: 50, width: 640, height: 400 };
  assert.deepEqual(pointAt(420, 250, r), { x: 0.5, y: 0.5 });
  assert.deepEqual(pointAt(0, 1000, r), { x: 0, y: 1 });
  assert.deepEqual(pointAt(100, 50, { ...r, width: 0, height: 0 }), { x: 0, y: 0 });
});

test("minutes left round up and never go negative", () => {
  const now = Date.parse("2026-10-06T10:00:00Z");
  assert.equal(minutesLeft("2026-10-06T10:29:01Z", now), 30);
  assert.equal(minutesLeft("2026-10-06T09:59:00Z", now), 0);
});

test("a parked or preparing handoff has no live page yet, but is not over", () => {
  assert.equal(isPending("parked"), true);
  assert.equal(isPending("preparing"), true);
  assert.equal(isOpen("preparing"), false);
  assert.equal(isPending("active") || isPending("done"), false);
});
