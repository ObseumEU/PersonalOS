// The reference patterns (src/refs.ts), the same examples as backend/tests/test_owner_report.py.
import assert from "node:assert/strict";
import { test } from "node:test";
import { findRefs, linkRefsMarkdown, parseRefHref } from "../src/refs.ts";

const CASES: [string, [string, string][]][] = [
  ["Result: note:23", [["note", "23"]]],
  ["Poznámka **23** obsahuje kontrolu", [["note", "23"]]],
  ["ne do poznámek 20 a 21", [["note", "20"], ["note", "21"]]],
  ["plánu (pozn. 20) a", [["note", "20"]]],
  ["hotové v note #6.", [["note", "6"]]],
  ["CEO dostal v DM (msg 1095) návrhy", [["msg", "1095"]]],
  ["(#obseum-ai msg 1088)", [["msg", "1088"]]],
  ["kanál 37, zpráva 504", [["msg", "504"]]],
  ["`firma.gh_0ff08113fa5428ee:c13`", [["chunk", "firma.gh_0ff08113fa5428ee:c13"]]],
  ["`k834E6OTGTM:c17`", [["chunk", "k834E6OTGTM:c17"]]],
  ["`kniha.gh_27a60d9ca94e73c3:c0/c2`", [["chunk", "kniha.gh_27a60d9ca94e73c3:c0"], ["chunk", "kniha.gh_27a60d9ca94e73c3:c2"]]],
  ["(XwZH-lOKG9c:c17, ryNgeWQSVuo:c0)", [["chunk", "XwZH-lOKG9c:c17"], ["chunk", "ryNgeWQSVuo:c0"]]],
  ["navazuje na T-431.", [["task", "T-431"]]],
  ["viz file:12", [["file", "12"]]],
  ["v 18:32, https://example.com/a:c12 a zprávy 3 dny", []],
];

for (const [text, want] of CASES)
  test(`findRefs: ${text}`, () => {
    assert.deepEqual(findRefs(text).map((r) => [r.kind, r.id]), want);
  });

test("linkRefsMarkdown turns references into preview links, code spans with ids too", () => {
  const md = linkRefsMarkdown("Poznámka **23** a citace `k834E6OTGTM:c17`, msg 1095, T-431 a `npm test`.");
  assert.match(md, /\[Poznámka 23\]\(#pos-ref\/note\/23\)/);
  assert.match(md, /\[citace\]\(#pos-ref\/chunk\/k834E6OTGTM%3Ac17\)/);
  assert.match(md, /\[msg 1095\]\(#pos-ref\/msg\/1095\)/);
  assert.match(md, /T-431/); // tasks are linked by the Markdown component (the task panel)
  assert.match(md, /`npm test`/);
  assert.equal(linkRefsMarkdown("[note:5](https://x.cz)"), "[note:5](https://x.cz)");
  assert.match(linkRefsMarkdown("ne do poznámek 20 a 21."), /poznámek \[20\]\(#pos-ref\/note\/20\) a \[21\]\(#pos-ref\/note\/21\)/);
  assert.deepEqual(parseRefHref("#pos-ref/chunk/k834E6OTGTM%3Ac17"), { kind: "chunk", id: "k834E6OTGTM:c17" });
  assert.equal(parseRefHref("/tasks/T-1"), null);
});
