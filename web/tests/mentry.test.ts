// The installed app's entry (/m, src/mobile/main.tsx) stays small: its static import graph (lazy
// import() does not count) must not reach the conversation screen and its heavy parts. Mirrors the
// gzip budget in scripts/check-pwa.mjs, but fails with the import path that pulled them in.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { test } from "node:test";

const SRC = resolve(import.meta.dirname, "../src");
const Q = "[\"']";
const STATIC = new RegExp(String.raw`^\s*(?:import|export)\s[^;]*?\sfrom\s+${Q}(\.[^"']+)${Q}|^\s*import\s+${Q}(\.[^"']+)${Q}`, "gm");

function file(base: string): string | null {
  for (const ext of ["", ".ts", ".tsx", "/index.ts", "/index.tsx"]) if (existsSync(base + ext) && !base.endsWith("/")) {
    const p = base + ext;
    if (/\.(tsx?|css)$/.test(p)) return p;
  }
  return null;
}

/** Each module statically reachable from the entry, with the module that imported it first. */
function graph(entry: string): Map<string, string | null> {
  const seen = new Map<string, string | null>([[entry, null]]);
  const queue = [entry];
  while (queue.length) {
    const cur = queue.shift()!;
    if (!/\.tsx?$/.test(cur)) continue;
    for (const m of readFileSync(cur, "utf8").matchAll(STATIC)) {
      if (/^\s*import\s+type\s/.test(m[0]) || /^\s*export\s+type\s/.test(m[0])) continue;
      const next = file(resolve(dirname(cur), m[1] ?? m[2]));
      if (next && !seen.has(next)) (seen.set(next, cur), queue.push(next));
    }
  }
  return seen;
}

const rel = (p: string) => p.slice(SRC.length + 1).split("\\").join("/");
function chain(g: Map<string, string | null>, p: string): string {
  const out = [rel(p)];
  for (let x = g.get(p); x; x = g.get(x)) out.unshift(rel(x));
  return out.join(" → ");
}

test("the /m entry does not statically import the conversation screen or the Messenger", () => {
  const g = graph(resolve(SRC, "mobile/main.tsx"));
  assert.ok(g.has(resolve(SRC, "mobile/ChatList.tsx")), "the start screen is in the entry");
  for (const heavy of ["mobile/Conversation.tsx", "mobile/Needs.tsx", "mobile/Tasks.tsx", "chat/Messenger.tsx",
    "chat/Rich.tsx", "files/FileCard.tsx", "components/RefPreview.tsx", "pages/ReportPage.tsx"]) {
    const p = resolve(SRC, heavy);
    assert.ok(!g.has(p), `${heavy} is in the /m entry: ${g.has(p) ? chain(g, p) : ""} (load it with import())`);
  }
});
