// Every key the web app looks up exists in the Czech dictionary, and no module looks one up while it
// loads (before the dictionary is registered, t() returns the raw key: the owner saw
// "work.priority.2" in the task panel). Run: npm test.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";

const SRC = join(import.meta.dirname, "..", "src");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(n) ? [p] : [];
  });
}

async function dictionary(): Promise<Set<string>> {
  const keys = new Set<string>();
  for (const f of files(join(SRC, "i18n", "cs"))) {
    const mod = await import(pathToFileURL(f).href);
    for (const k of Object.keys(mod.default)) keys.add(k);
  }
  return keys;
}

const code = files(SRC).filter((f) => !f.includes(join("i18n", "cs")));

/** Literal keys passed to t(): t("a.b"), t('a.b'), t(`a.b`), and both arms of t(x ? "a" : "b"). */
function usedKeys(src: string): string[] {
  const out: string[] = [];
  for (const m of src.matchAll(/(?<![\w.])t\(\s*(["'`])([a-z][\w-]*(?:\.[\w-]+)+)\1/g)) out.push(m[2]);
  for (const m of src.matchAll(/(?<![\w.])t\(\s*[^()"'`]*\?\s*"([a-z][\w-]*(?:\.[\w-]+)+)"\s*:\s*"([a-z][\w-]*(?:\.[\w-]+)+)"/g)) out.push(m[1], m[2]);
  return out;
}

test("every literal key used with t() exists in the Czech dictionary", async () => {
  const dict = await dictionary();
  const missing: string[] = [];
  for (const f of code) {
    const src = readFileSync(f, "utf8");
    for (const k of usedKeys(src)) if (!dict.has(k)) missing.push(`${relative(SRC, f)}: ${k}`);
  }
  assert.deepEqual([...new Set(missing)], []);
});

test("the priority labels and other shared labels are looked up when shown, not at import", () => {
  const offenders: string[] = [];
  for (const f of code) {
    const lines = readFileSync(f, "utf8").split("\n");
    for (let i = 0; i < lines.length; i++) {
      // A top-level declaration: from its line to the next line that starts at column 0.
      if (!/^(export\s+)?(const|let|var)\s/.test(lines[i])) continue;
      let j = i + 1;
      while (j < lines.length && (lines[j] === "" || /^[\s})\]]/.test(lines[j]))) j++;
      const stmt = lines.slice(i, j).join("\n");
      const at = stmt.search(/(?<![\w.])t\(\s*["'`]/);
      if (at < 0) continue;
      const before = stmt.slice(0, at);
      // Inside a function (arrow or function expression), t() runs later: fine.
      if (/=>|\bfunction\b|new Proxy\(|\bget\s+\w+\(\)/.test(before)) continue;
      offenders.push(`${relative(SRC, f)}:${i + 1}: ${lines[i].trim().slice(0, 80)}`);
    }
  }
  assert.deepEqual(offenders, []);
});
