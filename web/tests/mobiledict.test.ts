// The installed app (/m) registers only part of the dictionary up front (src/mobile/dict.ts); a key
// from another area shows raw ("approval.email_send") until the rest arrives. Every key the /m
// screens look up, and the modules outside /m they import, must be in the areas dict.ts registers
// or in an area that loads with the module itself (its static imports of i18n/cs/*, or of the whole
// dictionary, i18n/index). Literal t("a.b") keys must exist; for label("group", x) and
// t(`a.b.${x}`) the group must have keys. Run: npm test.
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "node:test";

const SRC = resolve(import.meta.dirname, "..", "src");
const MOBILE = join(SRC, "mobile");
const I18N = join(SRC, "i18n");
const CS = join(I18N, "cs");
const INDEX = join(I18N, "index.ts");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(n) ? [p] : [];
  });
}

function resolveImport(from: string, spec: string): string | null {
  const base = resolve(dirname(from), spec);
  for (const ext of [".ts", ".tsx", `${sep}index.ts`, `${sep}index.tsx`, ""]) if (/\.tsx?$/.test(base + ext) && existsSync(base + ext)) return base + ext;
  return null;
}

/** Static, non-type imports of relative modules (a lazy import() is not followed). */
function imports(f: string): string[] {
  const out: string[] = [];
  const re = /^\s*(?:import|export)\s+(?!type\s)[^;]*?\sfrom\s+["'](\.[^"']+)["']|^\s*import\s+["'](\.[^"']+)["']/gm;
  for (const m of readFileSync(f, "utf8").matchAll(re)) {
    const p = resolveImport(f, m[1] ?? m[2]);
    if (p) out.push(p);
  }
  return out;
}

async function areaKeys(paths: string[]): Promise<Set<string>> {
  const keys = new Set<string>();
  for (const p of paths) for (const k of Object.keys((await import(pathToFileURL(p).href)).default)) keys.add(k);
  return keys;
}

/** The dictionary areas a module imports directly (dict.ts). */
const ownAreas = (f: string) => imports(f).filter((p) => dirname(p) === CS);

/** The areas that are registered once a module has loaded: everything it reaches statically. */
function loadedAreas(f: string): string[] | "all" {
  const seen = new Set([f]);
  const queue = [f];
  const areas: string[] = [];
  while (queue.length) {
    for (const p of imports(queue.shift()!)) {
      if (p === INDEX) return "all";
      if (seen.has(p)) continue;
      seen.add(p);
      if (dirname(p) === CS) areas.push(p);
      else queue.push(p);
    }
  }
  return areas;
}

/** Groups of keys looked up with a variable part: label("approval", x), t(`m.tasks.view.${v}`). */
function usedGroups(src: string): string[] {
  const out: string[] = [];
  for (const m of src.matchAll(/(?<![\w.])label\(\s*"([a-z][\w.-]*)"/g)) out.push(`${m[1]}.`);
  for (const m of src.matchAll(/(?<![\w.])t\(\s*`([a-z][\w.-]*\.)\$\{/g)) out.push(m[1]);
  return out;
}

/** Literal keys passed to t(), and both arms of t(x ? "a" : "b") (as in i18n.test.ts). */
function usedKeys(src: string): string[] {
  const out: string[] = [];
  for (const m of src.matchAll(/(?<![\w.])t\(\s*(["'`])([a-z][\w-]*(?:\.[\w-]+)+)\1/g)) out.push(m[2]);
  for (const m of src.matchAll(/(?<![\w.])t\(\s*[^()"'`]*\?\s*"([a-z][\w-]*(?:\.[\w-]+)+)"\s*:\s*"([a-z][\w-]*(?:\.[\w-]+)+)"/g)) out.push(m[1], m[2]);
  return out;
}

test("the /m screens look up only keys that the installed app has registered", async () => {
  const registered = await areaKeys(ownAreas(join(MOBILE, "dict.ts")));
  assert.ok(registered.size > 100, "dict.ts registers the first screens' areas");
  const mobile = files(MOBILE);
  const outside = [...new Set(mobile.flatMap(imports))].filter((p) => !p.startsWith(MOBILE + sep) && !p.startsWith(I18N + sep));
  assert.ok(outside.length > 5, "the modules /m imports from the rest of the app are scanned too");
  const missing: string[] = [];
  for (const f of [...mobile, ...outside]) {
    const loaded = loadedAreas(f);
    if (loaded === "all") continue; // it loads the whole dictionary with itself
    const own = await areaKeys(loaded);
    const has = (k: string) => registered.has(k) || own.has(k);
    const keys = [...registered, ...own];
    const src = readFileSync(f, "utf8");
    const where = relative(SRC, f).split(sep).join("/");
    for (const k of usedKeys(src)) if (!has(k)) missing.push(`${where}: ${k}`);
    for (const g of usedGroups(src)) if (!keys.some((k) => k.startsWith(g))) missing.push(`${where}: ${g}*`);
  }
  assert.deepEqual([...new Set(missing)], []);
});
