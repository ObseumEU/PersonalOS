// Dictation joins the words into the box; attached files' lines are split off for display. Run: npm test.
import assert from "node:assert/strict";
import { test } from "node:test";
import { joinDictation, pastedName, splitFileLines } from "../src/composeText.ts";

test("dictated words go after what is in the box, one space between", () => {
  assert.equal(joinDictation("", "  dobrý   den "), "dobrý den");
  assert.equal(joinDictation("Ahoj", "jak se máš"), "Ahoj jak se máš");
  assert.equal(joinDictation("Ahoj\n", "druhý řádek"), "Ahoj\ndruhý řádek");
  assert.equal(joinDictation("beze změny", "   "), "beze změny");
});

test("a pasted picture gets a name with the time", () => {
  assert.equal(pastedName("image/png", new Date(2026, 9, 5, 14, 2, 33)), "snimek-20261005-140233.png");
  assert.equal(pastedName("image/jpeg", new Date(2026, 0, 1, 0, 0, 0)), "snimek-20260101-000000.jpg");
});

test("attached files' lines are split from the text", () => {
  const got = splitFileLines("Tohle je rozbité\n📎 snimek.png (soubor #14)\n📎 log.txt (soubor #15)");
  assert.equal(got.text, "Tohle je rozbité");
  assert.deepEqual(got.files, [{ id: 14, name: "snimek.png" }, { id: 15, name: "log.txt" }]);
  assert.deepEqual(splitFileLines("bez příloh"), { text: "bez příloh", files: [] });
});
