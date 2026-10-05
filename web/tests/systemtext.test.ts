// The platform's English notices read in Czech in chat (src/chat/systemText.ts).
import assert from "node:assert/strict";
import { test } from "node:test";
import { systemCs } from "../src/chat/systemText.ts";

test("known notices are said in Czech", () => {
  assert.equal(
    systemCs("Owner resolved your ask T-737 'Posunout kopii kódu': Provedu to dnes mimo špičku — I'm Continue T-662."),
    "Majitel odpověděl na dotaz T-737 „Posunout kopii kódu“: Provedu to dnes mimo špičku. Pokračuj na T-662.",
  );
  assert.equal(systemCs("CEO handed in T-391 'Plán' for your review. Read it."), "CEO předal T-391 „Plán“ ke kontrole.");
  assert.match(systemCs("Review digest: 40 result(s) wait for your review. Each one…"), /^Souhrn ke kontrole: 40 výsledků čeká/);
  assert.equal(systemCs("Ahoj, posílám plán."), "Ahoj, posílám plán.");
});

