// Dictation with a fake speech recogniser: Czech, live words into the box, stop, errors, no button without support. Run: npm test.
import assert from "node:assert/strict";
import { test } from "node:test";
import { joinDictation } from "../src/composeText.ts";
import { createDictation, recognitionClass, type Recognition, type SpeechEvent } from "../src/dictation.ts";

class FakeRec implements Recognition {
  static made: FakeRec[] = [];
  lang = "";
  continuous = false;
  interimResults = false;
  onresult: ((e: SpeechEvent) => void) | null = null;
  onerror: ((e: { error: string }) => void) | null = null;
  onend: (() => void) | null = null;
  started = 0;
  stopped = 0;
  constructor() {
    FakeRec.made.push(this);
  }
  start() {
    this.started++;
  }
  stop() {
    this.stopped++;
    this.onend?.();
  }
  abort() {}
  hear(...words: [string, boolean][]) {
    const results = Object.assign(words.map(([transcript, isFinal]) => Object.assign([{ transcript }], { isFinal })), {});
    this.onresult?.({ resultIndex: 0, results });
  }
}

function session(box: { text: string }, android = false) {
  FakeRec.made = [];
  const log = { errors: [] as string[], stops: 0 };
  const d = createDictation(
    FakeRec,
    {
      base: () => box.text,
      onText: (base, said) => (box.text = joinDictation(base, said)),
      onError: (e) => log.errors.push(e),
      onStop: () => log.stops++,
    },
    android,
  );
  return { d, log };
}

test("no recogniser, no dictation (the button hides); Chrome's prefixed one counts", () => {
  assert.equal(recognitionClass({}), null);
  assert.equal(recognitionClass(undefined), null);
  assert.equal(recognitionClass({ webkitSpeechRecognition: FakeRec }), FakeRec);
  assert.equal(recognitionClass({ SpeechRecognition: FakeRec }), FakeRec);
});

test("starts in Czech, words go live after the text in the box, a tap stops it", () => {
  const box = { text: "Ahoj" };
  const { d, log } = session(box);
  d.start();
  const r = FakeRec.made[0];
  assert.equal(r.lang, "cs-CZ");
  assert.equal(r.interimResults, true);
  assert.equal(r.continuous, true);
  assert.equal(r.started, 1);
  r.hear(["test dikto", false]);
  assert.equal(box.text, "Ahoj test dikto");
  r.hear(["test diktování", true]);
  assert.equal(box.text, "Ahoj test diktování");
  d.stop();
  assert.equal(r.stopped, 1);
  assert.equal(log.stops, 1);
  assert.deepEqual(log.errors, []);
});

test("the end of speech stops it too (iOS ends after a phrase)", () => {
  const { d, log } = session({ text: "" });
  d.start();
  FakeRec.made[0].onend?.();
  assert.equal(log.stops, 1);
  assert.equal(FakeRec.made.length, 1);
});

test("a refused microphone is reported once; silence is not an error", () => {
  const { d, log } = session({ text: "" });
  d.start();
  const r = FakeRec.made[0];
  r.onerror?.({ error: "no-speech" });
  assert.deepEqual(log.errors, []);
  r.onerror?.({ error: "not-allowed" });
  r.onend?.();
  assert.deepEqual(log.errors, ["not-allowed"]);
  assert.equal(log.stops, 1);
});

test("on Android it listens a phrase at a time and goes on after what is in the box", () => {
  const box = { text: "" };
  const { d, log } = session(box, true);
  d.start();
  assert.equal(FakeRec.made[0].continuous, false);
  FakeRec.made[0].hear(["první", true]);
  FakeRec.made[0].onend?.();
  assert.equal(FakeRec.made.length, 2);
  FakeRec.made[1].hear(["druhá", true]);
  assert.equal(box.text, "první druhá");
  d.stop();
  assert.equal(log.stops, 1);
  assert.equal(FakeRec.made.length, 2);
});
